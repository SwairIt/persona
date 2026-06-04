"""Today-vs-average dashboard widget — compare today's activity to a
7-day rolling average.

Built so the operator can glance at the top of ``/stats`` and answer
"am I more or less productive today than usual?". Five metrics, each
with today's value, the trailing 7-day average (the seven calendar
days *preceding* today — today itself is intentionally excluded from
the average so the comparison is "today vs the baseline week"), the
percentage delta of today versus that average, and a direction label
(``up``/``down``/``same``) the template can colour-code without doing
its own arithmetic.

Metrics
-------

1. ``screenshots``    — ``COUNT(*) FROM screenshots`` for the local day.
2. ``audio_seconds``  — ``SUM(duration_seconds) FROM audio_segment``.
3. ``apps``           — distinct ``app_name`` count from ``screenshots``.
4. ``window_titles``  — distinct ``window_title`` count from ``screenshots``.
5. ``idle_seconds``   — ``SUM(idle_seconds) FROM screenshots`` (Windows
   ``GetLastInputInfo``-derived; treated as 0 where the column is NULL
   on legacy rows).

Output shape
------------

``compute_today_vs_average()`` returns a plain ``dict`` rather than a
Pydantic model because the JSON endpoint and the HTMX partial both
consume the same structure — the dict *is* the public surface, and
keeping it plain means we don't have to maintain a model class
alongside the SQL.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.today_vs_average")

# 7-day trailing window. Captured as a module constant so the SQL bind
# value and the divisor in :func:`_avg_per_day` stay in lock-step.
_TRAILING_DAYS = 7

Direction = Literal["up", "down", "same"]


class MetricRow(TypedDict):
    name: str
    today: float
    avg7d: float
    delta_pct: float
    direction: Direction


class TodayVsAverage(TypedDict):
    today_iso: str
    metrics: list[MetricRow]


def _today_local() -> date:
    """Local-date "today" — matches the wall clock the operator sees.

    Mirrors :func:`app.web.routes.audio_stats._today_local` so the
    widget's "today" agrees with every other day-keyed page rather
    than drifting by one around midnight in non-UTC timezones.
    """
    return datetime.now().astimezone().date()


def _avg_per_day(total_over_window: float) -> float:
    """Divide a sum across the trailing window by :data:`_TRAILING_DAYS`.

    Kept as a tiny helper so the divisor is named — a bare ``/ 7`` in
    five places is a refactor hazard the day we want a configurable
    window.
    """
    return total_over_window / _TRAILING_DAYS


def _direction_for(delta_pct: float) -> Direction:
    """Bucket the delta into ``up``/``down``/``same`` for the template.

    Anything inside ±0.5 % collapses to ``same`` so noise around the
    average doesn't paint the row green or red — the operator's eye
    wants a real signal, not a flicker.
    """
    if delta_pct > 0.5:
        return "up"
    if delta_pct < -0.5:
        return "down"
    return "same"


def _delta_pct(today_value: float, avg_value: float) -> float:
    """Compute the percentage delta of today versus the 7-day average.

    Special-cases the zero-baseline edge: if the average is zero we
    return ``100.0`` when today is non-zero (any activity at all is
    "100 % more than the baseline of nothing") and ``0.0`` when today
    is also zero (no change). Rounded to one decimal so the template
    doesn't have to.
    """
    if avg_value <= 0.0:
        return 100.0 if today_value > 0.0 else 0.0
    return round(((today_value - avg_value) / avg_value) * 100.0, 1)


def _build_metric(name: str, today_value: float, avg_value: float) -> MetricRow:
    """Pack a single metric into the wire-shape consumed by the template."""
    delta = _delta_pct(today_value, avg_value)
    return MetricRow(
        name=name,
        today=today_value,
        avg7d=round(avg_value, 2),
        delta_pct=delta,
        direction=_direction_for(delta),
    )


async def compute_today_vs_average() -> TodayVsAverage:
    """Return today's value and 7-day-avg / delta for every tracked metric.

    Every query is parametrised — no string interpolation into SQL —
    and bounded by ``DATE(captured_at) = ?`` for today or ``BETWEEN ?
    AND ?`` for the trailing seven calendar days *before* today. That
    way the average is the operator's baseline week, not contaminated
    by today's partial-day numbers.
    """
    today = _today_local()
    window_end = today - timedelta(days=1)  # inclusive end of trailing window
    window_start = today - timedelta(days=_TRAILING_DAYS)  # inclusive start
    today_str = today.isoformat()
    window_start_str = window_start.isoformat()
    window_end_str = window_end.isoformat()

    async with get_connection() as conn:
        # Screenshots — today vs trailing 7-day total.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE DATE(captured_at) = ?",
            (today_str,),
        )
        row = await cursor.fetchone()
        today_shots = float(row["n"]) if row is not None else 0.0

        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE DATE(captured_at) BETWEEN ? AND ?",
            (window_start_str, window_end_str),
        )
        row = await cursor.fetchone()
        window_shots = float(row["n"]) if row is not None else 0.0

        # Audio seconds — sum of duration_seconds in audio_segment.
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total FROM audio_segment "
            "WHERE DATE(captured_at) = ?",
            (today_str,),
        )
        row = await cursor.fetchone()
        today_audio = float(row["total"]) if row is not None else 0.0

        cursor = await conn.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total FROM audio_segment "
            "WHERE DATE(captured_at) BETWEEN ? AND ?",
            (window_start_str, window_end_str),
        )
        row = await cursor.fetchone()
        window_audio = float(row["total"]) if row is not None else 0.0

        # Unique apps — distinct app_name count.
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT app_name) AS n FROM screenshots "
            "WHERE DATE(captured_at) = ? AND app_name IS NOT NULL AND app_name != ''",
            (today_str,),
        )
        row = await cursor.fetchone()
        today_apps = float(row["n"]) if row is not None else 0.0

        # Per-day average of distinct apps across the trailing window —
        # GROUP BY day then average, so a long-lived app that appears
        # every day counts once per day rather than once across the
        # whole week. COALESCE keeps the AVG defined when the window
        # has zero rows.
        cursor = await conn.execute(
            "SELECT COALESCE(AVG(daily_count), 0.0) AS avg_count FROM ("
            "  SELECT DATE(captured_at) AS day, "
            "         COUNT(DISTINCT app_name) AS daily_count "
            "    FROM screenshots "
            "   WHERE DATE(captured_at) BETWEEN ? AND ? "
            "     AND app_name IS NOT NULL AND app_name != '' "
            "   GROUP BY day"
            ")",
            (window_start_str, window_end_str),
        )
        row = await cursor.fetchone()
        avg_apps = float(row["avg_count"]) if row is not None else 0.0

        # Unique window titles — same shape as unique apps.
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT window_title) AS n FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "  AND window_title IS NOT NULL AND window_title != ''",
            (today_str,),
        )
        row = await cursor.fetchone()
        today_titles = float(row["n"]) if row is not None else 0.0

        cursor = await conn.execute(
            "SELECT COALESCE(AVG(daily_count), 0.0) AS avg_count FROM ("
            "  SELECT DATE(captured_at) AS day, "
            "         COUNT(DISTINCT window_title) AS daily_count "
            "    FROM screenshots "
            "   WHERE DATE(captured_at) BETWEEN ? AND ? "
            "     AND window_title IS NOT NULL AND window_title != '' "
            "   GROUP BY day"
            ")",
            (window_start_str, window_end_str),
        )
        row = await cursor.fetchone()
        avg_titles = float(row["avg_count"]) if row is not None else 0.0

        # Idle seconds — sum across all today's shots, then sum across
        # the trailing window. NULL values from legacy rows collapse to
        # zero via COALESCE on the column itself, not just the SUM, so
        # a row with NULL doesn't poison the total.
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(COALESCE(idle_seconds, 0)), 0) AS total "
            "FROM screenshots WHERE DATE(captured_at) = ?",
            (today_str,),
        )
        row = await cursor.fetchone()
        today_idle = float(row["total"]) if row is not None else 0.0

        cursor = await conn.execute(
            "SELECT COALESCE(SUM(COALESCE(idle_seconds, 0)), 0) AS total "
            "FROM screenshots WHERE DATE(captured_at) BETWEEN ? AND ?",
            (window_start_str, window_end_str),
        )
        row = await cursor.fetchone()
        window_idle = float(row["total"]) if row is not None else 0.0

    metrics: list[MetricRow] = [
        _build_metric("screenshots", today_shots, _avg_per_day(window_shots)),
        _build_metric("audio_seconds", today_audio, _avg_per_day(window_audio)),
        _build_metric("apps", today_apps, avg_apps),
        _build_metric("window_titles", today_titles, avg_titles),
        _build_metric("idle_seconds", today_idle, _avg_per_day(window_idle)),
    ]

    payload: TodayVsAverage = {
        "today_iso": today.isoformat(),
        "metrics": metrics,
    }

    log.info(
        "today_vs_average.computed",
        today=today.isoformat(),
        window_start=window_start_str,
        window_end=window_end_str,
        screenshots_today=today_shots,
        audio_seconds_today=today_audio,
        apps_today=today_apps,
        window_titles_today=today_titles,
        idle_seconds_today=today_idle,
    )

    return payload
