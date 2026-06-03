"""Weekly idle-vs-active rollup — 7 consecutive local days ending at
``end_date`` (default = today).

Thin layer on top of :func:`app.idle_stats.daily_idle`: walks the 7-day
window one day at a time, projecting each day's result down to the two
numbers the stacked-bar chart needs (``active_seconds`` and
``idle_seconds``). The output is a dense list — every day in the window
is always present, with zero seconds when no captures landed.

Returning a plain ``list[dict]`` lets the HTML page and the JSON endpoint
share a single source of truth without coupling to FastAPI or Jinja.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypedDict

from app.idle_stats import (
    DEFAULT_IDLE_THRESHOLD_S,
    DEFAULT_MAX_GAP_S,
    daily_idle,
)
from app.logging_setup import get_logger

log = get_logger("persona.idle_week")

WINDOW_DAYS = 7


class WeeklyIdleDay(TypedDict):
    date: str
    active_seconds: int
    idle_seconds: int


def _parse_end_date(end_date_iso: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to today on bad / missing input."""
    if end_date_iso:
        try:
            return date.fromisoformat(end_date_iso)
        except ValueError:
            log.warning("idle_week.bad_end_date", end_date_iso=end_date_iso)
    return datetime.now().astimezone().date()


async def weekly_idle(
    end_date_iso: str | None = None,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
    max_gap_s: int = DEFAULT_MAX_GAP_S,
) -> list[WeeklyIdleDay]:
    """Return a dense 7-entry active-vs-idle breakdown ending at ``end_date``.

    The list is ordered chronologically (oldest day first, ``end_date`` last)
    so chart callers can iterate left-to-right without re-sorting. Each entry
    carries the ISO date plus the two integer second-totals; everything else
    (counts, first / last capture) is intentionally dropped — the weekly
    surface stays narrow on purpose.
    """
    end_date = _parse_end_date(end_date_iso)
    start_date = end_date - timedelta(days=WINDOW_DAYS - 1)

    days: list[WeeklyIdleDay] = []
    for offset in range(WINDOW_DAYS):
        target = start_date + timedelta(days=offset)
        stats = await daily_idle(
            target.isoformat(),
            idle_threshold_s=idle_threshold_s,
            max_gap_s=max_gap_s,
        )
        days.append(
            WeeklyIdleDay(
                date=target.isoformat(),
                active_seconds=int(stats["active_seconds"]),
                idle_seconds=int(stats["idle_seconds"]),
            )
        )

    total_active = sum(d["active_seconds"] for d in days)
    total_idle = sum(d["idle_seconds"] for d in days)

    log.info(
        "idle_week.computed",
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        total_active_seconds=total_active,
        total_idle_seconds=total_idle,
        idle_threshold_s=idle_threshold_s,
        max_gap_s=max_gap_s,
    )
    return days
