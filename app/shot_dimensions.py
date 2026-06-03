"""Backfill missing ``width`` / ``height`` for legacy screenshot rows.

Modern Persona captures (v0.30+) write width/height at insert time
straight from the MSS grab result. Legacy rows from older deployments
have ``NULL`` in both columns once migration ``050`` runs, and the size
filter (``min_w`` / ``min_h`` on ``/search``) silently excludes them.

This module fills that gap. :func:`backfill_missing` walks the first
``limit`` rows where ``width IS NULL`` and opens each thumbnail via
:func:`PIL.Image.open` to read the pixel dimensions, then ``UPDATE``s
the row. PIL work runs inside :func:`anyio.to_thread.run_sync` so the
calling coroutine never blocks the event loop on disk IO.

A row is *skipped* (rather than failed) when there is nothing reasonable
to do:

* ``thumbnail_path`` is ``NULL`` — the original capture skipped the
  thumbnail (smart-min-gap suppression, or an older code path) and there
  is no on-disk artefact to inspect.
* The thumbnail file is missing — eviction, manual cleanup, or a
  partially-restored backup. We log it once and keep going.
* PIL refuses the file — corrupt write, foreign format, truncated bytes.

The skipped/updated/scanned tally is the single return value so the
admin route can render it without a second DB hit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import anyio
from PIL import Image, UnidentifiedImageError

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.dimensions")


class BackfillResult(TypedDict):
    """Tally returned by :func:`backfill_missing`.

    * ``scanned`` — rows the SELECT returned (``<= limit``).
    * ``updated`` — rows whose ``width`` / ``height`` we wrote.
    * ``skipped`` — rows we touched but could not measure (missing
      thumbnail, missing file, unreadable image).
    """

    scanned: int
    updated: int
    skipped: int


def _read_dimensions(thumb_path: Path) -> tuple[int, int] | None:
    """Open ``thumb_path`` with PIL and return ``(width, height)``.

    Runs in a worker thread via :func:`anyio.to_thread.run_sync` because
    PIL's ``Image.open`` is a synchronous disk read plus a header parse.
    Returns ``None`` when the file is missing or PIL refuses it — the
    caller turns that into a ``skipped`` tally bump.
    """
    if not thumb_path.exists():
        return None
    try:
        with Image.open(thumb_path) as image:
            image.load()
            width, height = image.size
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        log.warning(
            "dimensions.read_failed",
            path=str(thumb_path),
            error=str(exc),
        )
        return None
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


async def _select_pending(
    conn: aiosqlite.Connection,
    limit: int,
) -> list[tuple[int, str | None]]:
    """Return ``(id, thumbnail_path)`` for rows still missing ``width``."""
    cursor = await conn.execute(
        "SELECT id, thumbnail_path FROM screenshots "
        "WHERE width IS NULL "
        "ORDER BY id "
        "LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(int(row["id"]), row["thumbnail_path"]) for row in rows]


async def backfill_missing(limit: int = 500) -> BackfillResult:
    """Fill ``width`` / ``height`` for up to ``limit`` legacy rows.

    Returns a :class:`BackfillResult` tally. Safe to call repeatedly —
    each pass picks up where the previous one left off because rows with
    a non-NULL ``width`` drop out of the SELECT.

    ``limit`` is clamped to a sensible floor of ``1`` so a misconfigured
    caller (e.g. an admin form posting ``0``) cannot turn the call into a
    no-op that still hits SQLite.
    """
    effective_limit = max(1, int(limit))

    async with get_connection() as conn:
        pending = await _select_pending(conn, effective_limit)
        scanned = len(pending)
        updated = 0
        skipped = 0

        for shot_id, thumb_str in pending:
            if not thumb_str:
                skipped += 1
                continue
            thumb_path = Path(thumb_str)
            dims = await anyio.to_thread.run_sync(_read_dimensions, thumb_path)
            if dims is None:
                skipped += 1
                continue
            width, height = dims
            await conn.execute(
                "UPDATE screenshots SET width = ?, height = ? WHERE id = ?",
                (width, height, shot_id),
            )
            updated += 1

        await conn.commit()

    log.info(
        "dimensions.backfill",
        scanned=scanned,
        updated=updated,
        skipped=skipped,
        limit=effective_limit,
    )
    return BackfillResult(scanned=scanned, updated=updated, skipped=skipped)
