"""Yearly capture-activity heatmap — GitHub-style contribution grid.

Aggregates :class:`screenshots.captured_at` into a 365-day bucketed series
ending at ``end_date`` (default *today*), with one row per calendar day and a
discrete intensity ``level`` derived from percentile cut-offs across the
non-zero distribution.

The output is intentionally a plain ``dict`` (with strict typing via
:class:`HeatmapPayload`) so the HTML page and JSON endpoint can share a
single source of truth.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.heatmap")

# Span — GitHub's contribution graph is 53 columns by 7 rows = 371 cells, but
# the spec calls for "365 days ending at end_date" so we honour that exactly
# and let the template pad up to a full 53-column grid with empty leading
# cells before the start_date's column.
_DAYS_IN_YEAR = 365


class HeatmapDay(TypedDict):
    date: str
    count: int
    level: int


class HeatmapPayload(TypedDict):
    start_date: str
    end_date: str
    days: list[HeatmapDay]
    max_count: int
    total: int


def _parse_day(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` SQLite ``DATE()`` string into a :class:`date`."""
    return date.fromisoformat(value[:10])


def _percentile(sorted_values: list[int], pct: float) -> float:
    """Linear-interpolated percentile across a *sorted* ascending list.

    ``pct`` is in ``[0, 1]``.  Empty input returns ``0.0`` so callers can
    safely use the result as a numeric threshold.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = pct * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _level_for(count: int, p33: float, p66: float, p90: float) -> int:
    """Bucket a raw day-count into a discrete ``0..4`` intensity level.

    Empty days are always level 0; positive days start at level 1 even if the
    distribution is flat enough that the percentile cut-offs would otherwise
    swallow them.
    """
    if count <= 0:
        return 0
    if count > p90:
        return 4
    if count > p66:
        return 3
    if count > p33:
        return 2
    return 1


async def yearly_heatmap(end_date: date | None = None) -> HeatmapPayload:
    """Return a 365-day capture-activity heatmap ending at ``end_date``.

    The series is dense — every calendar day in the window is present with a
    ``count`` of ``0`` if no screenshots were captured on that day.

    Levels are derived from the *non-zero* day-count distribution so a quiet
    week of activity doesn't drown out genuinely heavy days:

    * level 0 — no captures
    * level 1 — ``1..pct33``
    * level 2 — ``pct33..pct66``
    * level 3 — ``pct66..pct90``
    * level 4 — ``> pct90``
    """
    anchor = end_date or datetime.now().astimezone().date()
    start = anchor - timedelta(days=_DAYS_IN_YEAR - 1)
    start_iso = start.isoformat()
    end_iso = anchor.isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND DATE(captured_at) BETWEEN ? AND ? "
            "GROUP BY day",
            (start_iso, end_iso),
        )
        raw_rows = await cursor.fetchall()

    counts: dict[date, int] = {}
    for row in raw_rows:
        raw_day = row["day"]
        if not raw_day:
            continue
        try:
            day = _parse_day(str(raw_day))
        except ValueError:
            log.warning("heatmap.bad_day_skipped", day=str(raw_day))
            continue
        counts[day] = counts.get(day, 0) + int(row["n"])

    non_zero_sorted: list[int] = sorted(v for v in counts.values() if v > 0)
    p33 = _percentile(non_zero_sorted, 0.33)
    p66 = _percentile(non_zero_sorted, 0.66)
    p90 = _percentile(non_zero_sorted, 0.90)

    days: list[HeatmapDay] = []
    total = 0
    max_count = 0
    for offset in range(_DAYS_IN_YEAR):
        current = start + timedelta(days=offset)
        n = counts.get(current, 0)
        max_count = max(max_count, n)
        total += n
        days.append(
            HeatmapDay(
                date=current.isoformat(),
                count=n,
                level=_level_for(n, p33, p66, p90),
            )
        )

    log.info(
        "heatmap.computed",
        start=start_iso,
        end=end_iso,
        total=total,
        max_count=max_count,
        non_zero_days=len(non_zero_sorted),
        p33=p33,
        p66=p66,
        p90=p90,
    )

    return HeatmapPayload(
        start_date=start_iso,
        end_date=end_iso,
        days=days,
        max_count=max_count,
        total=total,
    )
