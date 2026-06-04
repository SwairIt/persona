"""Helpers for the multi-monitor stacked thumbnail view.

When ``settings.multi_monitor`` is ``True`` the capture worker grabs
every connected monitor (see :func:`app.capture.screen.capture_all_monitors`)
and persists each grab as its own row in ``screenshots`` — one row per
monitor, all sharing the same ``captured_at`` UTC timestamp and each
with its own ``monitor_index``. The on-disk thumbnail convention is one
file per row at ``<thumbnails_dir>/YYYY-MM-DD/<screenshot_id>.webp``
(see :mod:`app.storage.thumbnails`); there is currently no
``_mon<N>`` filename suffix in the wild.

This module is the read-side adapter for that layout. Given any
single ``shot_id`` from a multi-monitor capture, :func:`list_monitor_thumbnails`
walks the thumbnails directory for any sibling files that *do* follow a
``<shot_id>_mon<N>.webp`` convention (kept as a forward-compatible hook
in case the writer ever switches to that scheme) and falls back to the
row's own ``thumbnail_path`` when no such siblings exist — so the
caller always gets at least ``[original_thumbnail_path]`` back, never
an empty list.

Pair this with :func:`list_monitor_screenshots` (DB-side: returns every
screenshot row sharing the row's ``captured_at`` timestamp) to render
the full per-physical-capture monitor stack — that is the path the
route in :mod:`app.web.routes.multi_monitor` actually uses, because in
the current writer convention every monitor lives in its own row.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

if TYPE_CHECKING:
    from app.storage.models import Screenshot

log = get_logger("persona.multi_monitor_view")

# Match ``<shot_id>_mon<N>.webp`` (e.g. ``42_mon0.webp``, ``42_mon1.webp``).
# Anchored on both ends so a row id of ``42`` never matches ``142_mon0.webp``.
_MON_SUFFIX_RE: re.Pattern[str] = re.compile(r"^(?P<shot>\d+)_mon(?P<idx>\d+)\.webp$")


def _parse_monitor_index(path: Path, shot_id: int) -> int | None:
    """Return the monitor index parsed from ``<shot_id>_mon<N>.webp``.

    Returns ``None`` when the filename does not match the convention or
    when the leading shot id is not the requested one. Callers use the
    ``None`` to filter out unrelated siblings before sorting.
    """
    match = _MON_SUFFIX_RE.match(path.name)
    if match is None:
        return None
    if int(match.group("shot")) != shot_id:
        return None
    return int(match.group("idx"))


def _scan_mon_suffix_siblings(parent: Path, shot_id: int) -> list[Path]:
    """Synchronous scan of ``parent`` for ``<shot_id>_mon<N>.webp`` files.

    Returned list is sorted by the parsed monitor index. Runs inside
    :func:`asyncio.to_thread` so the disk walk never blocks the loop.
    """
    if not parent.exists() or not parent.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for candidate in parent.iterdir():
        if not candidate.is_file():
            continue
        idx = _parse_monitor_index(candidate, shot_id)
        if idx is None:
            continue
        found.append((idx, candidate))
    found.sort(key=lambda pair: pair[0])
    return [path for _, path in found]


async def list_monitor_thumbnails(shot_id: int) -> list[Path]:
    """Return every on-disk thumbnail file related to ``shot_id``.

    The walk happens in the dated subfolder that holds the row's own
    thumbnail (``<thumbnails_dir>/YYYY-MM-DD/``) and matches the
    forward-compatible ``<shot_id>_mon<N>.webp`` convention. When no
    such siblings exist — the current writer reality — we fall back to
    a single-element list containing the row's own ``thumbnail_path``
    so the caller always has something to render.

    Returns an empty list only when the row itself is missing or has
    no recorded thumbnail and no on-disk siblings.
    """
    async with get_connection() as conn:
        row: Screenshot | None = await get_screenshot(conn, shot_id)

    if row is None:
        log.debug("multi_monitor_view.row_missing", shot_id=shot_id)
        return []

    original = Path(row.thumbnail_path) if row.thumbnail_path else None
    parent = original.parent if original is not None else None

    siblings: list[Path] = []
    if parent is not None:
        siblings = await asyncio.to_thread(_scan_mon_suffix_siblings, parent, shot_id)

    if siblings:
        log.debug(
            "multi_monitor_view.suffix_hit",
            shot_id=shot_id,
            count=len(siblings),
        )
        return siblings

    if original is not None and original.exists():
        log.debug("multi_monitor_view.single_fallback", shot_id=shot_id)
        return [original]

    log.debug("multi_monitor_view.no_thumb", shot_id=shot_id)
    return []


async def list_monitor_screenshots(shot_id: int) -> list[Screenshot]:
    """Return every screenshot row sharing the row's ``captured_at``.

    This is the DB-side companion to :func:`list_monitor_thumbnails`:
    the current capture writer persists each monitor as its own row at
    the same UTC timestamp (see :func:`app.capture.screen.capture_all_monitors`),
    so a multi-monitor "physical capture" is reconstructed by grouping
    on ``captured_at``. Rows are returned sorted by ``monitor_index``
    so the template renders them top-to-bottom in display order.

    Returns ``[]`` when the requested row is missing. Always returns at
    least the requested row itself when it exists, even on a
    single-monitor system.
    """
    async with get_connection() as conn:
        row: Screenshot | None = await get_screenshot(conn, shot_id)
        if row is None:
            log.debug("multi_monitor_view.row_missing", shot_id=shot_id)
            return []

        cursor = await conn.execute(
            """
            SELECT id FROM screenshots
            WHERE captured_at = ?
            ORDER BY monitor_index ASC, id ASC
            """,
            (row.captured_at.isoformat(),),
        )
        id_rows = await cursor.fetchall()
        sibling_ids: list[int] = [int(r["id"]) for r in id_rows]

        if shot_id not in sibling_ids:
            # Defensive fallback — the row exists but somehow the
            # timestamp-grouped lookup missed it (e.g. legacy non-ISO
            # serialisation). Return at least the requested row so the
            # caller still has something to render.
            log.debug("multi_monitor_view.timestamp_miss", shot_id=shot_id)
            return [row]

        siblings: list[Screenshot] = []
        for sibling_id in sibling_ids:
            fetched = await get_screenshot(conn, sibling_id)
            if fetched is not None:
                siblings.append(fetched)

    log.debug(
        "multi_monitor_view.siblings",
        shot_id=shot_id,
        count=len(siblings),
    )
    return siblings
