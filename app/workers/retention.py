"""Tier-sweep + retention worker.

For each screenshot older than warm_after_days: downsize its thumbnail
(if any) to a smaller dimension/quality and mark tier='warm'.

For each screenshot older than cold_after_days: delete its thumbnail and
mark tier='cold'. DB row and OCR text stay forever.

Pinned screenshots are skipped — they keep their hot thumbnail until the
user explicitly unpins.

Eventually (after `retention_days`) DB row stays but if you want hard delete
that's a separate scripts/cleanup_orphans.py + bulk-delete workflow.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from app import recycle
from app.app_retention import AppRetention, list_overrides
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import log_capture_event
from app.storage.size_log import sample_today
from app.storage.time import iso
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.retention")
per_app_log = get_logger("persona.retention.per_app")

CHECK_INTERVAL_SECONDS = 3600.0


async def run_retention_worker(controller: CaptureController | None = None) -> None:
    """Sweep tiers + size log once per hour."""
    ctrl = controller or get_controller()

    while not ctrl.stop_event.is_set():
        await beat("retention-worker")
        try:
            stats = await _sweep_once()
            if any(stats.values()):
                async with get_connection() as conn:
                    await log_capture_event(conn, "cleanup", stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("retention.sweep_failed", error=str(exc))

        # v0.40 — purge soft-deleted rows that have outlived the bin
        # window. Runs once per loop iteration, after the tier sweep, so
        # a failing tier scan never blocks the recycle clean-up.
        try:
            settings = get_settings()
            purged = await recycle.purge_expired(
                retention_days=settings.recycle_retention_days,
            )
            if purged:
                async with get_connection() as conn:
                    await log_capture_event(
                        conn,
                        "cleanup",
                        {"recycle_purged": purged},
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("retention.recycle_purge_failed", error=str(exc))

        try:
            await asyncio.wait_for(ctrl.stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def _sweep_once() -> dict[str, int]:
    """Demote hot→warm, warm→cold; sample today's bytes. Returns summary."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    warm_cutoff = now - timedelta(days=settings.tier_warm_after_days)
    cold_cutoff = now - timedelta(days=settings.tier_cold_after_days)

    # v0.49 — per-app retention overrides. We fetch the whole table once
    # at the start of the sweep so the worker remains a single-pass walk;
    # the table is tiny (one row per overridden app), so the cost is
    # negligible compared with the screenshot scans below.
    overrides = await list_overrides()
    overrides_by_app: dict[str, AppRetention] = {o["app_name"]: o for o in overrides}
    excluded_apps = sorted(overrides_by_app.keys())
    never_apps = [o["app_name"] for o in overrides if o["never_delete"]]
    if never_apps:
        per_app_log.info("retention.per_app.skip", apps=never_apps)

    promoted_to_warm = 0
    promoted_to_cold = 0
    bytes_saved = 0

    if settings.tiered_retention:
        promoted_to_warm, bytes_saved_w = await _demote_to_warm(
            warm_cutoff, excluded_apps
        )
        promoted_to_cold, bytes_saved_c = await _demote_to_cold(
            cold_cutoff, excluded_apps
        )
        bytes_saved = bytes_saved_w + bytes_saved_c

        # Per-app passes: each overridden app uses its own cutoffs, falling
        # back to the global cutoff where a column is NULL. Apps with
        # ``never_delete=1`` are skipped entirely (no demote, no delete).
        for app_name, override in overrides_by_app.items():
            if override["never_delete"]:
                continue
            app_warm_days = override["warm_after_days"]
            app_cold_days = override["cold_after_days"]
            app_warm_cutoff = (
                now - timedelta(days=app_warm_days)
                if app_warm_days is not None
                else warm_cutoff
            )
            app_cold_cutoff = (
                now - timedelta(days=app_cold_days)
                if app_cold_days is not None
                else cold_cutoff
            )
            warm_n, warm_bytes = await _demote_to_warm_for_app(
                app_warm_cutoff, app_name
            )
            cold_n, cold_bytes = await _demote_to_cold_for_app(
                app_cold_cutoff, app_name
            )
            promoted_to_warm += warm_n
            promoted_to_cold += cold_n
            bytes_saved += warm_bytes + cold_bytes
            if warm_n or cold_n:
                per_app_log.info(
                    "retention.per_app.swept",
                    app_name=app_name,
                    warm=warm_n,
                    cold=cold_n,
                    bytes_saved=warm_bytes + cold_bytes,
                )

    async with get_connection() as conn:
        await sample_today(conn, settings.thumbnails_dir)

    if promoted_to_warm or promoted_to_cold:
        log.info(
            "retention.swept",
            warm=promoted_to_warm,
            cold=promoted_to_cold,
            bytes_saved=bytes_saved,
        )

    return {
        "warm": promoted_to_warm,
        "cold": promoted_to_cold,
        "bytes_saved": bytes_saved,
    }


async def _demote_to_warm(
    cutoff: datetime,
    excluded_apps: list[str] | None = None,
) -> tuple[int, int]:
    """Downscale thumbnails for hot screenshots older than `cutoff`.

    ``excluded_apps`` is the set of app names that have a per-app
    retention override and are therefore handled by the per-app pass —
    we skip them here so an app with ``never_delete=1`` or a later
    custom cutoff is not picked up by the global query first.
    """
    settings = get_settings()
    excluded = excluded_apps or []
    base_sql = (
        "SELECT id, thumbnail_path FROM screenshots "
        "WHERE tier = 'hot' AND captured_at < ? AND thumbnail_path IS NOT NULL"
    )
    params: list[object] = [iso(cutoff)]
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        base_sql += (
            f" AND (app_name IS NULL OR app_name NOT IN ({placeholders}))"
        )
        params.extend(excluded)
    base_sql += " LIMIT 500"
    async with get_connection() as conn:
        cursor = await conn.execute(base_sql, tuple(params))
        rows = await cursor.fetchall()

    processed = 0
    bytes_saved = 0
    for row in rows:
        sid = int(row["id"])
        thumb = row["thumbnail_path"]
        if not thumb:
            continue
        path = Path(thumb)
        if not path.exists():
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE screenshots SET tier = 'warm', thumbnail_path = NULL WHERE id = ?",
                    (sid,),
                )
                await conn.commit()
            continue

        try:
            size_before = path.stat().st_size
            await asyncio.to_thread(
                _downscale,
                path,
                settings.tier_warm_thumbnail_width,
                settings.tier_warm_thumbnail_quality,
            )
            size_after = path.stat().st_size
            bytes_saved += max(0, size_before - size_after)
        except (OSError, Image.UnidentifiedImageError) as exc:
            log.warning("retention.demote_warm_failed", path=str(path), error=str(exc))
            continue

        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET tier = 'warm' WHERE id = ?",
                (sid,),
            )
            await conn.commit()
        processed += 1

    return processed, bytes_saved


async def _demote_to_cold(
    cutoff: datetime,
    excluded_apps: list[str] | None = None,
) -> tuple[int, int]:
    """Delete thumbnails for warm screenshots older than `cutoff`. Keep metadata.

    ``excluded_apps`` mirrors :func:`_demote_to_warm` — apps with a
    per-app override are handled separately to honour their custom
    cutoffs or the ``never_delete`` switch.
    """
    excluded = excluded_apps or []
    base_sql = (
        "SELECT id, thumbnail_path FROM screenshots "
        "WHERE tier IN ('hot', 'warm') AND captured_at < ?"
    )
    params: list[object] = [iso(cutoff)]
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        base_sql += (
            f" AND (app_name IS NULL OR app_name NOT IN ({placeholders}))"
        )
        params.extend(excluded)
    base_sql += " LIMIT 1000"
    async with get_connection() as conn:
        cursor = await conn.execute(base_sql, tuple(params))
        rows = await cursor.fetchall()

    deleted = 0
    bytes_saved = 0
    for row in rows:
        sid = int(row["id"])
        thumb = row["thumbnail_path"]
        if thumb:
            path = Path(thumb)
            if path.exists():
                try:
                    bytes_saved += path.stat().st_size
                    path.unlink()
                    deleted += 1
                except OSError as exc:
                    log.warning("retention.cold_delete_failed", path=str(path), error=str(exc))
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET tier = 'cold', thumbnail_path = NULL WHERE id = ?",
                (sid,),
            )
            await conn.commit()

    return deleted, bytes_saved


async def _demote_to_warm_for_app(
    cutoff: datetime,
    app_name: str,
) -> tuple[int, int]:
    """Per-app variant of :func:`_demote_to_warm`.

    Walks only screenshots whose ``app_name`` matches ``app_name`` (case-
    sensitive, same shape the capture loop persists). The logic is
    intentionally a thin copy of the global helper so the lean,
    single-purpose worker stays easy to read — the alternative (one
    helper with optional filters) hides the per-app pass behind a flag
    and is harder to debug.
    """
    settings = get_settings()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots "
            "WHERE tier = 'hot' AND captured_at < ? AND thumbnail_path IS NOT NULL "
            "AND app_name = ? "
            "LIMIT 500",
            (iso(cutoff), app_name),
        )
        rows = await cursor.fetchall()

    processed = 0
    bytes_saved = 0
    for row in rows:
        sid = int(row["id"])
        thumb = row["thumbnail_path"]
        if not thumb:
            continue
        path = Path(thumb)
        if not path.exists():
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE screenshots SET tier = 'warm', thumbnail_path = NULL WHERE id = ?",
                    (sid,),
                )
                await conn.commit()
            continue

        try:
            size_before = path.stat().st_size
            await asyncio.to_thread(
                _downscale,
                path,
                settings.tier_warm_thumbnail_width,
                settings.tier_warm_thumbnail_quality,
            )
            size_after = path.stat().st_size
            bytes_saved += max(0, size_before - size_after)
        except (OSError, Image.UnidentifiedImageError) as exc:
            per_app_log.warning(
                "retention.per_app.demote_warm_failed",
                app_name=app_name,
                path=str(path),
                error=str(exc),
            )
            continue

        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET tier = 'warm' WHERE id = ?",
                (sid,),
            )
            await conn.commit()
        processed += 1

    return processed, bytes_saved


async def _demote_to_cold_for_app(
    cutoff: datetime,
    app_name: str,
) -> tuple[int, int]:
    """Per-app variant of :func:`_demote_to_cold`."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots "
            "WHERE tier IN ('hot', 'warm') AND captured_at < ? "
            "AND app_name = ? "
            "LIMIT 1000",
            (iso(cutoff), app_name),
        )
        rows = await cursor.fetchall()

    deleted = 0
    bytes_saved = 0
    for row in rows:
        sid = int(row["id"])
        thumb = row["thumbnail_path"]
        if thumb:
            path = Path(thumb)
            if path.exists():
                try:
                    bytes_saved += path.stat().st_size
                    path.unlink()
                    deleted += 1
                except OSError as exc:
                    per_app_log.warning(
                        "retention.per_app.cold_delete_failed",
                        app_name=app_name,
                        path=str(path),
                        error=str(exc),
                    )
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET tier = 'cold', thumbnail_path = NULL WHERE id = ?",
                (sid,),
            )
            await conn.commit()

    return deleted, bytes_saved


def _downscale(path: Path, max_width: int, quality: int) -> None:
    with Image.open(path) as image:
        image.load()
        if image.width > max_width:
            ratio = max_width / image.width
            new_height = max(1, int(image.height * ratio))
            image = image.resize((max_width, new_height), Image.Resampling.LANCZOS)
        image.save(path, format="WEBP", quality=quality, method=6)
