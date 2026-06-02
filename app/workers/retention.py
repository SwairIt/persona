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
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import log_capture_event
from app.storage.size_log import sample_today
from app.storage.time import iso
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.retention")

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

    promoted_to_warm = 0
    promoted_to_cold = 0
    bytes_saved = 0

    if settings.tiered_retention:
        promoted_to_warm, bytes_saved_w = await _demote_to_warm(warm_cutoff)
        promoted_to_cold, bytes_saved_c = await _demote_to_cold(cold_cutoff)
        bytes_saved = bytes_saved_w + bytes_saved_c

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


async def _demote_to_warm(cutoff: datetime) -> tuple[int, int]:
    """Downscale thumbnails for hot screenshots older than `cutoff`."""
    settings = get_settings()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots "
            "WHERE tier = 'hot' AND captured_at < ? AND thumbnail_path IS NOT NULL "
            "LIMIT 500",
            (iso(cutoff),),
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


async def _demote_to_cold(cutoff: datetime) -> tuple[int, int]:
    """Delete thumbnails for warm screenshots older than `cutoff`. Keep metadata."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots "
            "WHERE tier IN ('hot', 'warm') AND captured_at < ? "
            "LIMIT 1000",
            (iso(cutoff),),
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
                    log.warning("retention.cold_delete_failed", path=str(path), error=str(exc))
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
