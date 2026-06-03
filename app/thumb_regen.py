"""Thumbnail regen CLI — heal rows whose ``thumbnail_path`` no longer resolves.

The capture pipeline writes one WebP per screenshot to
``data/thumbnails/YYYY-MM-DD/<id>.webp`` and stores the path in
``screenshots.thumbnail_path``. When that file disappears — operator
deleted the dated subfolder manually, a partial restore brought back
DB rows without the matching image tree, an external sync truncated
the folder, an antivirus quarantined the file — the row keeps
pointing at a path that 404s every time the timeline tries to render
it. :func:`regen_missing` scans up to ``limit`` rows, classifies each
``thumbnail_path`` against the live filesystem, and remediates the
dangling pointers.

Persona stores **only** thumbnails — there is no separate "original"
artifact on disk to re-encode from. When the WebP is gone the
on-disk source is gone with it; we cannot fabricate the pixels back.
The honest remediation is therefore:

* If the file is present — leave the row untouched. Nothing to regen
  (the existing helper :func:`app.storage.thumbnails.save_thumbnail`
  needs a source :class:`~PIL.Image.Image`, and the source for this
  shot IS the file we just confirmed). Counted under ``scanned``.
* If the file is missing — we cannot regenerate the pixels, so we
  clear ``thumbnail_path`` to ``NULL`` (so the timeline stops
  attempting to load the dead path) and count the row under
  ``failed``.

Both ``regenerated`` and ``failed`` always sum to "rows the SELECT
returned that had a missing file". The ``regenerated`` slot stays at
zero in the no-source-storage architecture — kept in the return
contract so a future v0.x that adds an originals tier can populate
it without breaking the CLI / route consumers.

All filesystem probes run inside :func:`anyio.to_thread.run_sync` so
the calling coroutine never blocks the event loop on disk IO.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import anyio

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.thumb_regen")


class RegenResult(TypedDict):
    """Tally returned by :func:`regen_missing`.

    * ``scanned`` — rows the SELECT returned (``<= limit``).
    * ``regenerated`` — rows whose thumbnail was successfully
      re-written from a recovered source. Always ``0`` in the
      current no-source-storage architecture; reserved for a future
      originals tier.
    * ``failed`` — rows whose ``thumbnail_path`` was missing on disk
      and could not be regenerated. Their pointer is cleared to
      ``NULL`` so the timeline stops trying to load a dead path.
    """

    scanned: int
    regenerated: int
    failed: int


def _file_exists(path: Path) -> bool:
    """Return ``True`` iff ``path`` resolves to an existing file.

    Runs in a worker thread via :func:`anyio.to_thread.run_sync`. A
    raced eviction between the SELECT and this probe simply returns
    ``False`` — that row will be classified as missing and have its
    pointer cleared, which is exactly what we want for an absent
    file regardless of when it vanished.
    """
    try:
        return path.is_file()
    except OSError as exc:
        # Permission denied, path-too-long, broken junction — treat
        # the same as "missing" from the consumer's point of view so
        # the row stops linking at an unreachable resource.
        log.warning("thumb_regen.stat_failed", path=str(path), error=str(exc))
        return False


async def _select_candidates(
    conn: aiosqlite.Connection,
    limit: int,
) -> list[tuple[int, str]]:
    """Return ``(id, thumbnail_path)`` for rows that still have a thumbnail path."""
    cursor = await conn.execute(
        "SELECT id, thumbnail_path FROM screenshots "
        "WHERE thumbnail_path IS NOT NULL "
        "ORDER BY id "
        "LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(int(row["id"]), str(row["thumbnail_path"])) for row in rows]


async def _clear_thumbnail_pointer(
    conn: aiosqlite.Connection,
    shot_id: int,
) -> None:
    """Null ``thumbnail_path`` for a row whose on-disk file is gone."""
    await conn.execute(
        "UPDATE screenshots SET thumbnail_path = NULL WHERE id = ?",
        (shot_id,),
    )


async def regen_missing(limit: int = 500) -> RegenResult:
    """Scan up to ``limit`` rows and remediate dangling ``thumbnail_path`` entries.

    Returns a :class:`RegenResult` tally. The function is safe to call
    repeatedly — once a row's dangling pointer is cleared it drops
    out of the candidate SELECT on the next pass, so an operator can
    page through a backlog by re-clicking the admin button until
    ``failed`` reads zero.

    ``limit`` is clamped to a floor of ``1`` so a misconfigured caller
    (admin form posting ``0``, CLI ``--limit 0``) cannot turn the
    call into a no-op that still touches SQLite.
    """
    effective_limit = max(1, int(limit))

    scanned = 0
    regenerated = 0
    failed = 0

    async with get_connection() as conn:
        candidates = await _select_candidates(conn, effective_limit)
        scanned = len(candidates)

        for shot_id, thumb_str in candidates:
            thumb_path = Path(thumb_str)
            exists = await anyio.to_thread.run_sync(_file_exists, thumb_path)
            if exists:
                # File still on disk; nothing to remediate. Not a
                # success in the regen sense — there was nothing to
                # regen — but also not a failure. Stays counted under
                # ``scanned`` only.
                continue

            # File is gone. Persona has no separate "original" to
            # re-encode from, so we cannot put pixels back. Clear the
            # dead pointer so the timeline stops trying to load it
            # and count the row as failed.
            await _clear_thumbnail_pointer(conn, shot_id)
            failed += 1

        await conn.commit()

    log.info(
        "thumb_regen.scan",
        scanned=scanned,
        regenerated=regenerated,
        failed=failed,
        limit=effective_limit,
    )
    return RegenResult(scanned=scanned, regenerated=regenerated, failed=failed)
