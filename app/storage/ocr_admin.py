"""Admin helpers for re-queuing OCR work on existing screenshots.

The OCR worker only touches rows with ``ocr_status = 'pending'``. These helpers
flip ``skipped`` / ``failed`` rows back to ``pending`` so the worker picks them
up on the next sweep — useful after installing Tesseract, swapping language
packs, or fixing a corrupted result.

All helpers refuse to reset rows without a ``thumbnail_path`` because there's
nothing for the OCR worker to read in that case.
"""

from __future__ import annotations

import aiosqlite

from app.logging_setup import get_logger

logger = get_logger(__name__)


async def reset_skipped_to_pending(conn: aiosqlite.Connection) -> int:
    """Flip ``ocr_status='skipped'`` rows (with a thumbnail) back to ``pending``.

    Returns the number of rows updated.
    """
    cursor = await conn.execute(
        "UPDATE screenshots SET ocr_status = 'pending' "
        "WHERE ocr_status = 'skipped' AND thumbnail_path IS NOT NULL"
    )
    await conn.commit()
    affected = cursor.rowcount or 0
    logger.info("ocr_admin.reset_skipped", rows=affected)
    return affected


async def reset_failed_to_pending(conn: aiosqlite.Connection) -> int:
    """Flip ``ocr_status='failed'`` rows (with a thumbnail) back to ``pending``.

    Returns the number of rows updated.
    """
    cursor = await conn.execute(
        "UPDATE screenshots SET ocr_status = 'pending' "
        "WHERE ocr_status = 'failed' AND thumbnail_path IS NOT NULL"
    )
    await conn.commit()
    affected = cursor.rowcount or 0
    logger.info("ocr_admin.reset_failed", rows=affected)
    return affected


async def reset_all_to_pending(conn: aiosqlite.Connection) -> int:
    """Reset both ``skipped`` and ``failed`` rows to ``pending``.

    Returns the total number of rows updated.
    """
    cursor = await conn.execute(
        "UPDATE screenshots SET ocr_status = 'pending' "
        "WHERE ocr_status IN ('skipped', 'failed') AND thumbnail_path IS NOT NULL"
    )
    await conn.commit()
    affected = cursor.rowcount or 0
    logger.info("ocr_admin.reset_all", rows=affected)
    return affected


async def status_breakdown(conn: aiosqlite.Connection) -> dict[str, int]:
    """Return a ``{status: count}`` map covering every OCR bucket.

    The result always contains keys for ``pending``, ``done``, ``skipped`` and
    ``failed`` so templates can index them safely even when a bucket is empty.
    """
    cursor = await conn.execute(
        "SELECT ocr_status, COUNT(*) AS n FROM screenshots GROUP BY ocr_status"
    )
    rows = await cursor.fetchall()
    counts: dict[str, int] = {"pending": 0, "done": 0, "skipped": 0, "failed": 0}
    for row in rows:
        counts[str(row["ocr_status"])] = int(row["n"])
    counts["total"] = sum(value for key, value in counts.items() if key != "total")
    return counts


async def reset_one(conn: aiosqlite.Connection, screenshot_id: int) -> bool:
    """Reset a single screenshot back to ``pending``.

    Returns ``True`` when the row was updated, ``False`` if the row is missing
    or has no ``thumbnail_path`` (so OCR could not run anyway).
    """
    cursor = await conn.execute(
        "UPDATE screenshots SET ocr_status = 'pending' "
        "WHERE id = ? AND thumbnail_path IS NOT NULL",
        (screenshot_id,),
    )
    await conn.commit()
    success = (cursor.rowcount or 0) > 0
    logger.info("ocr_admin.reset_one", screenshot_id=screenshot_id, reset=success)
    return success
