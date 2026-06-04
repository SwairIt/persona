"""365-day GitHub-style activity heatmap — yearly capture overview.

Builds a dense 365-day series of screenshot counts ending at *today*
(UTC, derived inside SQLite via ``DATE('now')`` for parity with the
``captured_at`` storage format) plus a quartile-based intensity tier so
the template can render a fixed 5-colour scale without any client-side
maths.

The output is a plain ``dict`` (not a Pydantic model) because both the
HTML page and the ``/api/activity/year.json`` endpoint serialise it
straight to the wire — the structure is the public API.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.activity_heatmap")

# Spec: 365 calendar days ending today, left-filled with zero where no
# screenshots landed. Kept as a module-level constant so the SQL bind
# value and the Python ``range`` stay in lock-step.
_DEFAULT_DAYS = 365


class ActivityDay(TypedDict):
    date: str
    count: int
    tier: int


class ActivityHeatmap(TypedDict):
    days: list[ActivityDay]
    total_shots: int
    total_days_with_activity: int
    max_day_count: int
    streak_current: int
    streak_longest: int


def _tier_for(count: int, q25: float, q50: float, q75: float, peak: int) -> int:
    """Bucket a raw day-count into the 0..4 intensity tier.

    The spec is explicit:

    * 0 — zero shots
    * 1 — ``> 0`` up to the 25th percentile (inclusive)
    * 2 — above q25 up to the 50th percentile
    * 3 — above q50 up to the 75th percentile
    * 4 — above q75, capped at the observed maximum

    A single non-zero day flattens the distribution so q25/q50/q75 all
    collapse to that lone value; in that case every positive day lands
    at tier 4 which matches the "this is my busiest" intuition.
    """
    if count <= 0:
        return 0
    if count > q75 or count >= peak:
        return 4
    if count > q50:
        return 3
    if count > q25:
        return 2
    return 1


def _percentile(sorted_values: list[int], pct: float) -> float:
    """Linear-interpolated percentile across an ascending ``sorted_values``.

    ``pct`` is in ``[0, 1]``. Returns ``0.0`` for an empty input so
    callers can use the result as a numeric threshold without a
    pre-check.
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


def _today_utc() -> date:
    """Return today's calendar date in UTC.

    ``captured_at`` is stored as a UTC ISO string (see
    :func:`app.storage.time.iso`), so the heatmap window must be anchored
    in UTC too — anchoring in local time produces an off-by-one tail row
    around midnight in any non-UTC timezone.
    """
    return datetime.now(tz=UTC).date()


async def build_year_heatmap(days: int = _DEFAULT_DAYS) -> ActivityHeatmap:
    """Return a dense 365-day capture-activity heatmap ending today.

    The window length is parametric so tests can build short series, but
    the production caller always uses the 365-day default to match the
    7-row by 53-column SVG grid the template renders.

    The SQL filter uses SQLite's ``DATE('now', '-N days')`` modifier so
    the lower bound is computed inside the database — no Python
    date-arithmetic detour through ``?`` parameters that would have to
    re-encode as ISO strings. Both modifier args are still parameters
    rather than string-interpolation to keep the call ``ruff`` /
    static-analysis-clean.
    """
    if days <= 0:
        raise ValueError("days must be positive")

    anchor = _today_utc()
    start = anchor - timedelta(days=days - 1)
    span_modifier = f"-{days - 1} days"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND captured_at >= DATE(?, ?) "
            "GROUP BY day",
            ("now", span_modifier),
        )
        rows = await cursor.fetchall()

    counts: dict[date, int] = {}
    for row in rows:
        raw_day = row["day"]
        if not raw_day:
            continue
        try:
            day = date.fromisoformat(str(raw_day)[:10])
        except ValueError:
            log.warning("activity.bad_day_skipped", day=str(raw_day))
            continue
        # Drop anything that slipped through outside the requested window
        # (DATE(?, ?) returns calendar-day strings so the comparison is
        # well-defined, but a stray captured_at='' would still produce a
        # NULL day above and be skipped already).
        if day < start or day > anchor:
            continue
        counts[day] = counts.get(day, 0) + int(row["n"])

    non_zero_sorted: list[int] = sorted(v for v in counts.values() if v > 0)
    q25 = _percentile(non_zero_sorted, 0.25)
    q50 = _percentile(non_zero_sorted, 0.50)
    q75 = _percentile(non_zero_sorted, 0.75)
    peak = max(non_zero_sorted) if non_zero_sorted else 0

    series: list[ActivityDay] = []
    total_shots = 0
    days_with_activity = 0
    for offset in range(days):
        current = start + timedelta(days=offset)
        n = counts.get(current, 0)
        if n > 0:
            days_with_activity += 1
        total_shots += n
        series.append(
            ActivityDay(
                date=current.isoformat(),
                count=n,
                tier=_tier_for(n, q25, q50, q75, peak),
            )
        )

    streak_current = _current_streak(series)
    streak_longest = _longest_streak(series)

    payload: ActivityHeatmap = {
        "days": series,
        "total_shots": total_shots,
        "total_days_with_activity": days_with_activity,
        "max_day_count": peak,
        "streak_current": streak_current,
        "streak_longest": streak_longest,
    }

    log.info(
        "activity.computed",
        window_days=days,
        total_shots=total_shots,
        days_with_activity=days_with_activity,
        max_day_count=peak,
        streak_current=streak_current,
        streak_longest=streak_longest,
    )

    return payload


def _current_streak(series: list[ActivityDay]) -> int:
    """Count consecutive active days ending at the final entry.

    "Active" means ``count > 0``. The series is in ascending date order,
    so we walk it from the tail and stop at the first zero.
    """
    streak = 0
    for entry in reversed(series):
        if entry["count"] <= 0:
            break
        streak += 1
    return streak


def _longest_streak(series: list[ActivityDay]) -> int:
    """Return the longest run of consecutive active days in the window."""
    best = 0
    run = 0
    for entry in series:
        if entry["count"] > 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best
