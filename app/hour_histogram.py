"""Hour-of-day capture-activity histogram.

Buckets :class:`screenshots.captured_at` timestamps into 24 hour-of-day bins
across a configurable trailing window (default 30 days) and returns a dense
list of dicts — every hour ``0..23`` is always present, with a ``count`` of
``0`` when no screenshots landed in that bucket.

The output is a plain ``list[dict]`` so the HTML page and JSON endpoint can
share a single source of truth without coupling to Jinja or FastAPI.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.hours")

_HOURS_PER_DAY = 24
_MIN_DAYS = 1
_MAX_DAYS = 3650  # ~10 years — keeps the SQL modifier safely bounded.


class HourBucket(TypedDict):
    hour: int
    count: int
    pct: float


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


async def hourly_distribution(days: int = 30) -> list[HourBucket]:
    """Return a dense 24-entry hour-of-day histogram for the last ``days`` days.

    Each entry is a :class:`HourBucket` with:

    * ``hour`` — integer ``0..23``
    * ``count`` — total screenshots captured in that hour-of-day bucket
    * ``pct`` — share of the grand total as a percentage in ``[0.0, 100.0]``;
      ``0.0`` when the window is empty so callers can use it as a numeric
      width without a guard.

    Zero-count hours are filled in so the histogram is always 24 wide and the
    template can iterate without missing-key checks.
    """
    window = _clamp_days(days)
    modifier = f"-{window} days"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT CAST(strftime('%H', captured_at) AS INTEGER) AS hr, "
            "COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND captured_at >= date('now', ?) "
            "GROUP BY hr "
            "ORDER BY hr",
            (modifier,),
        )
        raw_rows = await cursor.fetchall()

    counts: dict[int, int] = {}
    for row in raw_rows:
        raw_hour = row["hr"]
        if raw_hour is None:
            continue
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            log.warning("hours.bad_hour_skipped", hour=str(raw_hour))
            continue
        if not 0 <= hour < _HOURS_PER_DAY:
            log.warning("hours.out_of_range_skipped", hour=hour)
            continue
        counts[hour] = counts.get(hour, 0) + int(row["n"])

    total = sum(counts.values())
    buckets: list[HourBucket] = []
    for hour in range(_HOURS_PER_DAY):
        n = counts.get(hour, 0)
        pct = (n / total * 100.0) if total else 0.0
        buckets.append(HourBucket(hour=hour, count=n, pct=pct))

    log.info(
        "hours.computed",
        days=window,
        total=total,
        non_zero_hours=sum(1 for b in buckets if b["count"] > 0),
    )

    return buckets
