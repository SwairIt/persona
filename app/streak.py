"""Daily capture-streak metric — Duolingo-style consecutive-day badge.

Computes the current consecutive-day run of screenshot captures ending today
(or yesterday if today has no captures), along with the longest streak ever
recorded and a small set of supporting stats.

This is a deliberately thin, dict-returning surface kept separate from the
richer :mod:`app.analysis.streak` summary so the streak page / JSON endpoint
can stay decoupled from the stats dashboard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.streak")


class StreakPayload(TypedDict):
    days: int
    longest: int
    last_capture_date: str | None
    today_count: int


def _parse_day(value: str) -> date:
    """Parse a ``YYYY-MM-DD`` SQLite ``DATE()`` string into a :class:`date`."""
    return date.fromisoformat(value[:10])


def _fold_rows(rows: list[tuple[str, int]]) -> tuple[dict[date, int], list[date]]:
    """Fold raw ``(day_str, count)`` rows into a date map + newest-first list."""
    counts: dict[date, int] = {}
    days_desc: list[date] = []
    for raw_day, raw_count in rows:
        if not raw_day:
            continue
        day = _parse_day(str(raw_day))
        if day in counts:
            counts[day] += int(raw_count)
        else:
            counts[day] = int(raw_count)
            days_desc.append(day)
    return counts, days_desc


def _walk_current(days_desc: list[date], anchor: date) -> int:
    """Count consecutive days back from ``anchor`` through ``days_desc``."""
    current = 0
    prev = anchor
    for day in days_desc:
        if day > anchor:
            # Future-dated rows (clock skew / timezone weirdness) — skip.
            continue
        if day != prev:
            break
        current += 1
        prev = prev - timedelta(days=1)
    return current


def _walk_longest(days: list[date]) -> int:
    """Longest consecutive run anywhere in the chronologically-sorted list."""
    longest = 0
    run = 0
    previous: date | None = None
    for day in days:
        run = run + 1 if previous is not None and day == previous + timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    return longest


async def current_streak() -> StreakPayload:
    """Return the current daily capture streak and supporting stats.

    Algorithm:

    1. Group screenshots by ``DATE(captured_at)`` and order newest-first.
    2. Walk the (descending) days; the current streak is the number of
       consecutive days back from today before the first gap.  If today has
       no captures we still allow yesterday to anchor the streak — losing the
       streak only happens once a full calendar day has been skipped.
    3. Independently compute the longest consecutive run anywhere in history.
    4. ``today_count`` is today's row count (``0`` if missing).
    5. ``last_capture_date`` is the most recent day, or ``None`` on an empty
       database.
    """
    today = datetime.now().astimezone().date()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "GROUP BY day "
            "ORDER BY day DESC"
        )
        raw_rows = await cursor.fetchall()

    rows: list[tuple[str, int]] = [(str(r["day"]), int(r["n"])) for r in raw_rows]
    counts, days_desc = _fold_rows(rows)

    if not days_desc:
        log.info("streak.empty", today=today.isoformat())
        return StreakPayload(days=0, longest=0, last_capture_date=None, today_count=0)

    today_count = counts.get(today, 0)
    last_capture_date = days_desc[0].isoformat()

    # Anchor today if it has captures, else yesterday (so the streak survives
    # a still-in-progress day with no captures yet).
    anchor = today if today in counts else today - timedelta(days=1)
    current = _walk_current(days_desc, anchor)
    longest = _walk_longest(sorted(counts))

    log.info(
        "streak.computed",
        days=current,
        longest=longest,
        today_count=today_count,
        last_capture_date=last_capture_date,
    )

    return StreakPayload(
        days=current,
        longest=longest,
        last_capture_date=last_capture_date,
        today_count=today_count,
    )
