"""Weekly capture-volume trend — v1.9 feature 2/3.

Aggregates :class:`screenshots.captured_at` into ISO-week buckets and
returns a dense list of ``{week_start, shots}`` rows covering the trailing
``weeks`` calendar weeks (default 26 — roughly half a year). Empty weeks
are emitted with ``shots = 0`` so chart callers can iterate left-to-right
without missing-key guards.

Design notes
------------
* **ISO-8601 weeks.** SQLite's ``strftime('%Y-%W', ...)`` returns the
  zero-based "week of year (Sunday-start)", which doesn't line up with
  the ISO definition. We compute the ISO year + week + Monday-anchored
  ``week_start`` in Python after pulling raw ``captured_at`` dates, which
  also dodges the year-boundary edge case (ISO week 1 of 2027 can
  legitimately contain December 2026 dates).
* **Dense window.** We build the full list of Monday anchors first and
  seed each with ``0``; the SQL result only ever adds to existing rows.
  Weeks the database has no rows for are still present.
* **Parametrised SQL.** The cutoff date is bound as a ``?`` parameter
  even though it's computed server-side — keeps the query shape uniform
  with the rest of Persona's read-only stats modules.
* **Read-only.** No inserts, updates or deletes happen here.

The output is a plain ``list[WeeklyCaptureBucket]`` so the HTML page and
the JSON endpoint can share a single source of truth without coupling to
Jinja or FastAPI.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.weekly_trend")

_MIN_WEEKS: Final[int] = 1
_MAX_WEEKS: Final[int] = 520  # ~10 years — same safety bound as the rest of Persona.
_DEFAULT_WEEKS: Final[int] = 26
_ISO_DATE_FMT: Final[str] = "%Y-%m-%d"


class WeeklyCaptureBucket(TypedDict):
    """One ISO-week's screenshot count.

    * ``week_start`` — ISO date string (``YYYY-MM-DD``) for the Monday
      that anchors the week. Always Monday-aligned regardless of the
      caller's locale.
    * ``shots`` — total ``screenshots`` rows whose ``captured_at`` falls
      inside that ISO week. ``0`` for empty weeks.
    """

    week_start: str
    shots: int


def _clamp_weeks(weeks: int) -> int:
    """Clamp ``weeks`` into ``[_MIN_WEEKS, _MAX_WEEKS]``.

    Mirrors the route-side guard so direct callers (CLI, tests) cannot
    blow past the safe window either.
    """
    if weeks < _MIN_WEEKS:
        return _MIN_WEEKS
    if weeks > _MAX_WEEKS:
        return _MAX_WEEKS
    return weeks


def _monday_of(target: date) -> date:
    """Return the Monday of the ISO week containing ``target``."""
    return target - timedelta(days=target.weekday())


def _dense_week_anchors(weeks: int, today: date | None = None) -> list[date]:
    """Build ``weeks`` Monday anchors ending at the week containing ``today``.

    The list is oldest-first so the SVG renderer can walk it
    left-to-right without re-sorting.
    """
    end_monday = _monday_of(today or date.today())
    start_monday = end_monday - timedelta(weeks=weeks - 1)
    return [start_monday + timedelta(weeks=offset) for offset in range(weeks)]


async def weekly_counts(
    weeks: int = _DEFAULT_WEEKS,
    today: date | None = None,
) -> list[WeeklyCaptureBucket]:
    """Return a dense ``weeks``-entry ISO-week capture trend.

    Each entry is a :class:`WeeklyCaptureBucket` with the Monday anchor
    (``week_start``) and the count of ``screenshots`` rows captured in
    that ISO week (``shots``). Weeks with no captures are still emitted
    with ``shots = 0`` so the chart never sees a sparse list.

    On a transient :class:`aiosqlite.Error` we log and return a dense
    all-zero window — the trend page should degrade gracefully rather
    than blow up the whole route.
    """
    window = _clamp_weeks(weeks)
    anchors = _dense_week_anchors(window, today=today)
    # Index by ISO ``YYYY-MM-DD`` Monday string so the SQL merge below
    # is a straight dict lookup with no date-comparison guards.
    buckets: dict[str, int] = {anchor.strftime(_ISO_DATE_FMT): 0 for anchor in anchors}
    cutoff = anchors[0].strftime(_ISO_DATE_FMT)

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
                "FROM screenshots "
                "WHERE captured_at IS NOT NULL "
                "AND DATE(captured_at) >= ? "
                "GROUP BY day",
                (cutoff,),
            )
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        log.warning("weekly_trend.query_failed", error=str(exc))
        return [
            WeeklyCaptureBucket(week_start=iso, shots=0)
            for iso in buckets
        ]

    for row in rows:
        raw_day = row["day"]
        if raw_day is None:
            continue
        day_str = str(raw_day)
        try:
            parsed = datetime.strptime(day_str, _ISO_DATE_FMT).date()
        except ValueError:
            log.warning("weekly_trend.bad_day_skipped", day=day_str)
            continue
        monday_key = _monday_of(parsed).strftime(_ISO_DATE_FMT)
        if monday_key not in buckets:
            # Row sits outside the dense window (shouldn't happen given
            # the ``DATE(...) >= ?`` filter, but defensive against tz
            # drift and clock-skew on the capture side).
            continue
        try:
            n = int(row["n"])
        except (TypeError, ValueError):
            log.warning("weekly_trend.bad_count_skipped", n=str(row["n"]))
            continue
        buckets[monday_key] += n

    dense: list[WeeklyCaptureBucket] = [
        WeeklyCaptureBucket(
            week_start=anchor.strftime(_ISO_DATE_FMT),
            shots=buckets[anchor.strftime(_ISO_DATE_FMT)],
        )
        for anchor in anchors
    ]

    total = sum(b["shots"] for b in dense)
    log.info(
        "weekly_trend.computed",
        weeks=window,
        total_shots=total,
        non_zero_weeks=sum(1 for b in dense if b["shots"] > 0),
    )

    return dense


__all__ = [
    "WeeklyCaptureBucket",
    "weekly_counts",
]
