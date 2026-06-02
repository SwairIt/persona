"""Per-day storage breakdown — screenshots, thumbnails bytes, OCR bytes.

Powers the ``/storage-report`` UI so the user can see whether the on-disk
footprint stays under the daily target (~2-4 MB/day).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

import anyio

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class DailyBreakdown(TypedDict):
    """One day's storage footprint, newest-first when listed."""

    date: str
    screenshots: int
    thumbnails_bytes: int
    ocr_bytes: int
    total_bytes: int


async def daily_breakdown(days_back: int = 30) -> list[DailyBreakdown]:
    """Return per-day storage usage for the last ``days_back`` days.

    Each entry combines three sources:

    * ``screenshots`` — ``COUNT(*)`` of rows with ``date(captured_at) = day``
    * ``thumbnails_bytes`` — sum of file sizes under
      ``data/thumbnails/<day>/`` (walked off-loop via :mod:`anyio`)
    * ``ocr_bytes`` — ``SUM(LENGTH(ocr_text))`` for screenshots of that day
      (approximates the OCR text storage cost)

    The returned list is sorted newest first and always contains exactly
    ``days_back`` entries — days with no activity are reported as zeros so
    the sparkline rendering stays uniform.
    """
    if days_back <= 0:
        return []

    settings = get_settings()
    thumbs_root = settings.thumbnails_dir

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=days_back - 1)
    cutoff_iso = iso(datetime.combine(start_day, datetime.min.time(), tzinfo=UTC))

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, "
            "       COUNT(*) AS shots, "
            "       COALESCE(SUM(LENGTH(ocr_text)), 0) AS ocr_bytes "
            "FROM screenshots "
            "WHERE captured_at >= ? "
            "GROUP BY day",
            (cutoff_iso,),
        )
        rows = await cursor.fetchall()

    by_day: dict[str, tuple[int, int]] = {
        str(row["day"]): (int(row["shots"]), int(row["ocr_bytes"])) for row in rows
    }

    thumbs_by_day = await anyio.to_thread.run_sync(
        _measure_thumbnails_per_day, thumbs_root, start_day, today
    )

    breakdown: list[DailyBreakdown] = []
    cursor_day = today
    while cursor_day >= start_day:
        key = cursor_day.isoformat()
        shots, ocr_bytes = by_day.get(key, (0, 0))
        thumb_bytes = thumbs_by_day.get(key, 0)
        breakdown.append(
            DailyBreakdown(
                date=key,
                screenshots=shots,
                thumbnails_bytes=thumb_bytes,
                ocr_bytes=ocr_bytes,
                total_bytes=thumb_bytes + ocr_bytes,
            )
        )
        cursor_day -= timedelta(days=1)

    logger.debug(
        "storage_report.daily_breakdown",
        days=days_back,
        rows=len(breakdown),
        thumbs_root=str(thumbs_root),
    )
    return breakdown


def _measure_thumbnails_per_day(
    root: Path, start_day: date, end_day: date
) -> dict[str, int]:
    """Sum file sizes under ``root`` grouped by the dated subdirectory.

    Supports both layouts seen in the codebase:

    * ``root/YYYY-MM-DD/<file>``  (current production layout)
    * ``root/YYYY/MM/DD/<file>``  (legacy / spec layout)

    Anything outside ``[start_day, end_day]`` or that does not parse as a
    date is skipped quietly so a long-lived install does not re-stat
    years of archive on each request.
    """
    sizes: dict[str, int] = {}
    if not root.exists():
        return sizes

    window: set[str] = set()
    cursor = start_day
    while cursor <= end_day:
        window.add(cursor.isoformat())
        cursor += timedelta(days=1)

    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if len(name) == 10 and name in window:
            sizes[name] = sizes.get(name, 0) + _sum_files(entry)
            continue
        if len(name) == 4 and name.isdigit():
            _collect_nested_year(entry, window, sizes)

    return sizes


def _collect_nested_year(
    year_dir: Path, window: set[str], sizes: dict[str, int]
) -> None:
    """Walk a ``YYYY/MM/DD`` tree, adding sizes to ``sizes`` for in-window days."""
    year = year_dir.name
    for month_dir in year_dir.iterdir():
        if not month_dir.is_dir() or len(month_dir.name) != 2 or not month_dir.name.isdigit():
            continue
        month = month_dir.name
        for day_dir in month_dir.iterdir():
            if (
                not day_dir.is_dir()
                or len(day_dir.name) != 2
                or not day_dir.name.isdigit()
            ):
                continue
            key = f"{year}-{month}-{day_dir.name}"
            if key not in window:
                continue
            sizes[key] = sizes.get(key, 0) + _sum_files(day_dir)


def _sum_files(folder: Path) -> int:
    total = 0
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


__all__ = ["DailyBreakdown", "daily_breakdown"]
