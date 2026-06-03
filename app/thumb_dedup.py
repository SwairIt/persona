"""On-disk thumbnail dedup — collapse byte-identical thumbnail files.

The capture pipeline writes one JPEG per screenshot regardless of
content. When the screen is static (idle desktop, modal dialog, locked
screen, app-switch animation tail frames) the same raw pixels land on
disk under many distinct paths and quietly multiply the thumbnail
footprint by the duration of the static stretch.

:func:`scan_and_dedup` walks up to ``limit`` ``screenshots`` rows whose
``thumbnail_path`` is set, hashes each file with SHA-256, and:

* If the digest is brand new — registers ``(sha256, path, size_bytes)``
  in :sql:`thumb_content` with ``ref_count = 1``. The screenshot keeps
  its current path; that path *becomes* the canonical one for this
  content.
* If the digest is already registered — rewrites
  ``screenshots.thumbnail_path`` to the canonical path, bumps the
  catalogue's ``ref_count``, and unlinks the now-redundant file.

The bytes saved on each collapse equal the duplicate file's size at the
moment it was unlinked; we accumulate that into ``bytes_freed`` and
return it to the caller so the admin page can render a meaningful "you
just reclaimed N MB" line.

All filesystem work (``stat``, hashing, ``unlink``) runs inside
:func:`anyio.to_thread.run_sync` so the calling coroutine never blocks
the event loop on disk IO. Catalogue rows that already point to a
missing canonical file are skipped, not repaired — a stale catalogue
entry is a separate cleanup concern and silently overwriting a valid
``screenshots.thumbnail_path`` with a dangling reference would be worse
than leaving the duplicate on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import anyio

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage_savings import record_thumb_dedup

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.thumb_dedup")

# Hash the file in 1 MiB slabs. Thumbnails are typically well under 1 MB
# so most files hash in a single read, but the chunked path keeps the
# memory ceiling bounded if a future capture profile produces large
# (multi-MB) thumbnails.
_HASH_CHUNK_BYTES = 1024 * 1024


class DedupResult(TypedDict):
    """Tally returned by :func:`scan_and_dedup`.

    * ``scanned`` — rows the SELECT returned (``<= limit``).
    * ``dedups`` — rows whose thumbnail was redirected to a canonical
      path and whose duplicate file was unlinked from disk.
    * ``bytes_freed`` — total bytes reclaimed by unlinking duplicates.
    """

    scanned: int
    dedups: int
    bytes_freed: int


def _hash_file(path: Path) -> tuple[str, int] | None:
    """Return ``(sha256_hex, size_bytes)`` for ``path``.

    Runs in a worker thread via :func:`anyio.to_thread.run_sync`. Reads
    the file in 1 MiB slabs so the resident memory cost stays bounded
    regardless of thumbnail size. ``None`` on any IO failure (missing
    file, permission denied, race with eviction) so the caller turns it
    into a skipped row instead of a 500.
    """
    try:
        size_bytes = path.stat().st_size
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError as exc:
        log.warning("thumb_dedup.hash_failed", path=str(path), error=str(exc))
        return None
    return hasher.hexdigest(), int(size_bytes)


def _unlink_file(path: Path) -> bool:
    """Delete ``path`` from disk. Return ``True`` iff the call removed it.

    Tolerates a missing file (``FileNotFoundError``) because a concurrent
    eviction sweep could have raced us to the same target — the dedup
    pass still counts that screenshot as collapsed because the DB row
    was rewritten and there is no longer a duplicate on disk.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("thumb_dedup.unlink_failed", path=str(path), error=str(exc))
        return False
    return True


async def _select_pending(
    conn: aiosqlite.Connection,
    limit: int,
) -> list[tuple[int, str]]:
    """Return ``(id, thumbnail_path)`` for rows that still have a thumbnail."""
    cursor = await conn.execute(
        "SELECT id, thumbnail_path FROM screenshots "
        "WHERE thumbnail_path IS NOT NULL "
        "ORDER BY id "
        "LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [(int(row["id"]), str(row["thumbnail_path"])) for row in rows]


async def _lookup_canonical(
    conn: aiosqlite.Connection,
    digest: str,
) -> str | None:
    """Return the canonical path stored under ``digest``, or ``None``."""
    cursor = await conn.execute(
        "SELECT path FROM thumb_content WHERE sha256 = ?",
        (digest,),
    )
    row = await cursor.fetchone()
    return None if row is None else str(row["path"])


async def _register_canonical(
    conn: aiosqlite.Connection,
    digest: str,
    path: str,
    size_bytes: int,
) -> None:
    """Insert a brand-new content row with ``ref_count = 1``."""
    await conn.execute(
        "INSERT INTO thumb_content (sha256, path, size_bytes, ref_count) "
        "VALUES (?, ?, ?, 1)",
        (digest, path, size_bytes),
    )


async def _bump_ref_count(conn: aiosqlite.Connection, digest: str) -> None:
    """Increment ``ref_count`` for an already-known digest."""
    await conn.execute(
        "UPDATE thumb_content SET ref_count = ref_count + 1 WHERE sha256 = ?",
        (digest,),
    )


async def _redirect_screenshot(
    conn: aiosqlite.Connection,
    shot_id: int,
    canonical_path: str,
) -> None:
    """Point a screenshot's ``thumbnail_path`` at the canonical file."""
    await conn.execute(
        "UPDATE screenshots SET thumbnail_path = ? WHERE id = ?",
        (canonical_path, shot_id),
    )


async def scan_and_dedup(limit: int = 500) -> DedupResult:
    """Scan up to ``limit`` thumbnails and collapse byte-identical ones.

    Returns a :class:`DedupResult` tally with the bytes reclaimed by
    unlinking duplicate files. Safe to call repeatedly — a screenshot
    whose thumbnail was already redirected to the canonical path still
    appears in the SELECT but its hash matches the registered canonical
    digest, the path comparison short-circuits, and nothing is unlinked.

    ``limit`` is clamped to a floor of ``1`` so a misconfigured caller
    (e.g. an admin form posting ``0``) cannot turn the call into a
    no-op that still touches SQLite.
    """
    effective_limit = max(1, int(limit))

    scanned = 0
    dedups = 0
    bytes_freed = 0

    async with get_connection() as conn:
        pending = await _select_pending(conn, effective_limit)
        scanned = len(pending)

        for shot_id, thumb_str in pending:
            thumb_path = Path(thumb_str)
            hash_result = await anyio.to_thread.run_sync(_hash_file, thumb_path)
            if hash_result is None:
                continue
            digest, size_bytes = hash_result

            canonical_path = await _lookup_canonical(conn, digest)

            if canonical_path is None:
                # First sighting — this file becomes the canonical copy.
                await _register_canonical(conn, digest, thumb_str, size_bytes)
                continue

            if canonical_path == thumb_str:
                # Already the canonical file; nothing to redirect or unlink.
                continue

            # Distinct path with identical content — collapse onto the
            # canonical entry. Rewrite the DB pointer first so a crash
            # between UPDATE and unlink leaves us with a still-valid
            # ``thumbnail_path`` pointing at the canonical file rather
            # than a dangling reference to the about-to-be-deleted file.
            await _redirect_screenshot(conn, shot_id, canonical_path)
            await _bump_ref_count(conn, digest)
            unlinked = await anyio.to_thread.run_sync(_unlink_file, thumb_path)
            if unlinked:
                bytes_freed += size_bytes
            dedups += 1

        await conn.commit()

    log.info(
        "thumb_dedup.scan",
        scanned=scanned,
        dedups=dedups,
        bytes_freed=bytes_freed,
        limit=effective_limit,
    )
    # Credit the savings journal *after* the scan transaction commits so
    # a rollback inside ``scan_and_dedup`` cannot leave a phantom bump
    # in ``storage_saving``. ``record_thumb_dedup`` no-ops on zero.
    await record_thumb_dedup(bytes_freed)
    return DedupResult(scanned=scanned, dedups=dedups, bytes_freed=bytes_freed)
