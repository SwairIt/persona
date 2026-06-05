"""Yearly Wrapped — Spotify-style year-in-review aggregations.

One public coroutine, :func:`compute_yearly_wrapped`, scans the four
core capture tables for a single calendar year and returns a single
``dict`` packed with everything the ``/wrapped`` page (and its sibling
``/api/wrapped/{year}.json`` endpoint) needs to render.

The metrics are intentionally cheap — a handful of grouped aggregates
over ``screenshots`` plus three small per-table totals. Each query is
fully parametrised; the only operator-supplied value (``year``) is
coerced to ``int`` by FastAPI's path validation before it reaches this
module, and we still bind it as two ISO-date strings so a malformed
caller can never inject SQL.

All numeric outputs are concrete Python ``int`` / ``float`` so the JSON
twin route can :func:`json.dumps` the result without any custom encoder.
Empty-year edge cases collapse to ``0`` / ``None`` / ``[]`` so the
template never has to guard against ``KeyError`` or ``NoneType.x``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import pairwise
from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.yearly_wrapped")

# Top-N caps used by the Spotify-style "your top 5" cards. Kept as
# module constants so the template's bar list and chip list stay in
# lockstep with the SQL ``LIMIT`` clauses below.
_TOP_APPS_LIMIT: Final[int] = 5
_TOP_TAGS_LIMIT: Final[int] = 5


def _year_bounds(year: int) -> tuple[str, str]:
    """Return ``(start_iso, end_iso)`` covering ``year`` in UTC.

    The end bound is the first instant of the *next* year so the SQL
    ``<`` comparison naturally drops 31-Dec-23:59:59.999 captures into
    the current year regardless of sub-second precision.
    """
    start = datetime(year, 1, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC)
    return iso(start), iso(end)


def _longest_consecutive_streak(active_days: list[str]) -> int:
    """Return the longest run of consecutive ISO ``YYYY-MM-DD`` days.

    ``active_days`` is expected to be sorted ascending (the SQL caller
    orders it that way). We re-parse defensively because SQLite's
    ``DATE()`` can occasionally hand back odd values when the source
    column is malformed — those rows are skipped rather than aborting
    the whole computation.
    """
    if not active_days:
        return 0
    parsed: list[date] = []
    for raw in active_days:
        try:
            parsed.append(date.fromisoformat(raw))
        except ValueError:
            log.debug("yearly_wrapped.bad_day", value=raw)
            continue
    if not parsed:
        return 0
    parsed.sort()
    longest = 1
    run = 1
    for prev, curr in pairwise(parsed):
        if (curr - prev).days == 1:
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return longest


async def _screenshot_totals(
    conn: Any, start_iso: str, end_iso: str
) -> tuple[int, int]:
    """Return ``(total_shots, total_unique_apps)`` for the window."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    total_shots = int(row["n"]) if row else 0

    cursor = await conn.execute(
        "SELECT COUNT(DISTINCT app_name) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "AND app_name IS NOT NULL AND app_name != ''",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    total_unique_apps = int(row["n"]) if row else 0
    return total_shots, total_unique_apps


async def _top_apps(
    conn: Any, start_iso: str, end_iso: str
) -> list[dict[str, Any]]:
    """Return the top-N apps by shot count in the window."""
    cursor = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "AND app_name IS NOT NULL AND app_name != '' "
        "GROUP BY app_name ORDER BY n DESC LIMIT ?",
        (start_iso, end_iso, _TOP_APPS_LIMIT),
    )
    rows = await cursor.fetchall()
    return [{"app": str(r["app_name"]), "shots": int(r["n"])} for r in rows]


async def _top_tags(
    conn: Any, start_iso: str, end_iso: str
) -> list[dict[str, Any]]:
    """Return the top-N tags by usage in the window."""
    cursor = await conn.execute(
        "SELECT tags.name AS name, COUNT(*) AS n "
        "FROM screenshot_tags "
        "JOIN tags ON tags.id = screenshot_tags.tag_id "
        "JOIN screenshots ON screenshots.id = screenshot_tags.screenshot_id "
        "WHERE screenshots.captured_at >= ? AND screenshots.captured_at < ? "
        "GROUP BY tags.name ORDER BY n DESC, tags.name ASC LIMIT ?",
        (start_iso, end_iso, _TOP_TAGS_LIMIT),
    )
    rows = await cursor.fetchall()
    return [{"tag": str(r["name"]), "uses": int(r["n"])} for r in rows]


async def _busiest_day(
    conn: Any, start_iso: str, end_iso: str
) -> dict[str, Any] | None:
    """Return ``{"day", "shot_count"}`` for the busiest day or None."""
    cursor = await conn.execute(
        "SELECT DATE(captured_at) AS d, COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "GROUP BY d ORDER BY n DESC, d ASC LIMIT 1",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None or row["d"] is None:
        return None
    return {"day": str(row["d"]), "shot_count": int(row["n"])}


async def _busiest_hour(
    conn: Any, start_iso: str, end_iso: str
) -> int | None:
    """Return the busiest hour-of-day (0-23) or None for an empty window."""
    cursor = await conn.execute(
        "SELECT CAST(strftime('%H', captured_at) AS INTEGER) AS hr, COUNT(*) AS n "
        "FROM screenshots WHERE captured_at >= ? AND captured_at < ? "
        "GROUP BY hr ORDER BY n DESC, hr ASC LIMIT 1",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None or row["hr"] is None:
        return None
    return int(row["hr"])


async def _capture_bounds(
    conn: Any, start_iso: str, end_iso: str
) -> tuple[str | None, str | None, int]:
    """Return ``(first_date, last_date, active_days_count)``."""
    cursor = await conn.execute(
        "SELECT MIN(DATE(captured_at)) AS first_d, "
        "MAX(DATE(captured_at)) AS last_d, "
        "COUNT(DISTINCT DATE(captured_at)) AS active "
        "FROM screenshots WHERE captured_at >= ? AND captured_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None:
        return None, None, 0
    first_d = str(row["first_d"]) if row["first_d"] else None
    last_d = str(row["last_d"]) if row["last_d"] else None
    active = int(row["active"]) if row["active"] else 0
    return first_d, last_d, active


async def _active_day_list(
    conn: Any, start_iso: str, end_iso: str
) -> list[str]:
    """Return the sorted list of distinct ISO active days in the window."""
    cursor = await conn.execute(
        "SELECT DISTINCT DATE(captured_at) AS d FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "ORDER BY d ASC",
        (start_iso, end_iso),
    )
    rows = await cursor.fetchall()
    return [str(r["d"]) for r in rows if r["d"] is not None]


async def _voice_hours(conn: Any, start_iso: str, end_iso: str) -> float:
    """Return the total recorded audio in the window, expressed in hours."""
    # v1.66 — schema has ``audio_segment.duration_seconds`` and
    # ``captured_at``, not ``duration_s``/``started_at`` (the v1.54 agent
    # named them after a different table).
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(duration_seconds), 0.0) AS secs FROM audio_segment "
        "WHERE captured_at >= ? AND captured_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    secs = float(row["secs"]) if row and row["secs"] is not None else 0.0
    return round(secs / 3600.0, 2)


async def _notes_count(conn: Any, start_iso: str, end_iso: str) -> int:
    """Return the count of notes created in the window."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM notes "
        "WHERE created_at >= ? AND created_at < ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def _longest_focus_row(
    conn: Any, start_iso: str, end_iso: str
) -> tuple[int, int, str, str] | None:
    """Return ``(id, duration_minutes, started_at, ended_at)`` or None."""
    cursor = await conn.execute(
        "SELECT id, duration_minutes, started_at, ended_at FROM focus_sessions "
        "WHERE started_at >= ? AND started_at < ? "
        "AND duration_minutes IS NOT NULL "
        "ORDER BY duration_minutes DESC LIMIT 1",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    ended_raw = row["ended_at"]
    return (
        int(row["id"]),
        int(row["duration_minutes"]),
        str(row["started_at"]),
        str(ended_raw) if ended_raw is not None else "",
    )


async def _query_focus_dominant_app(session_id: int, started_at: str, ended_at: str) -> str | None:
    """Return the app with the most screenshots overlapping a focus session.

    ``focus_sessions`` (the legacy plural table) does not carry an app
    name, so we approximate "dominant_app" by counting screenshots whose
    ``captured_at`` falls inside the session window. Returns ``None`` on
    an open-ended or empty session.
    """
    if not ended_at:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "AND app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT 1",
            (started_at, ended_at),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    log.debug(
        "yearly_wrapped.focus_dominant",
        session_id=session_id,
        app=row["app_name"],
        shots=int(row["n"]),
    )
    return str(row["app_name"])


async def compute_yearly_wrapped(year: int) -> dict[str, Any]:
    """Compute the full year-in-review payload for ``year``.

    Returns a dict with the following shape (zero/None on empty year):

    * ``year`` — echo of the input
    * ``total_shots`` / ``total_voice_hours`` / ``total_unique_apps`` /
      ``total_notes`` — top-line counters
    * ``top_5_apps`` — list of ``{"app": str, "shots": int}``
    * ``top_5_tags`` — list of ``{"tag": str, "uses": int}``
    * ``busiest_day`` — ``{"day": "YYYY-MM-DD", "shot_count": int}`` or None
    * ``busiest_hour_of_day`` — int 0-23 or None
    * ``longest_focus_session`` — ``{"id", "duration_minutes",
      "dominant_app"}`` or None
    * ``first_capture_date`` / ``last_capture_date`` — ISO date or None
    * ``total_active_days`` — int
    * ``average_daily_shots`` — float (shots / active_days, 0.0 if none)
    * ``longest_active_streak`` — int (the quirky stat)
    """
    start_iso, end_iso = _year_bounds(year)

    async with get_connection() as conn:
        total_shots, total_unique_apps = await _screenshot_totals(conn, start_iso, end_iso)
        top_5_apps = await _top_apps(conn, start_iso, end_iso)
        busiest_day = await _busiest_day(conn, start_iso, end_iso)
        busiest_hour_of_day = await _busiest_hour(conn, start_iso, end_iso)
        first_capture_date, last_capture_date, total_active_days = await _capture_bounds(
            conn, start_iso, end_iso
        )
        active_day_list = await _active_day_list(conn, start_iso, end_iso)
        top_5_tags = await _top_tags(conn, start_iso, end_iso)
        total_voice_hours = await _voice_hours(conn, start_iso, end_iso)
        total_notes = await _notes_count(conn, start_iso, end_iso)
        focus_tuple = await _longest_focus_row(conn, start_iso, end_iso)

    longest_focus_session: dict[str, Any] | None = None
    if focus_tuple is not None:
        focus_id, duration_minutes, started_at, ended_at = focus_tuple
        dominant_app = await _query_focus_dominant_app(focus_id, started_at, ended_at)
        longest_focus_session = {
            "id": focus_id,
            "duration_minutes": duration_minutes,
            "dominant_app": dominant_app,
        }

    average_daily_shots = (
        round(total_shots / total_active_days, 2) if total_active_days > 0 else 0.0
    )
    longest_active_streak = _longest_consecutive_streak(active_day_list)

    payload: dict[str, Any] = {
        "year": int(year),
        "total_shots": total_shots,
        "total_voice_hours": total_voice_hours,
        "total_unique_apps": total_unique_apps,
        "total_notes": total_notes,
        "top_5_apps": top_5_apps,
        "top_5_tags": top_5_tags,
        "busiest_day": busiest_day,
        "busiest_hour_of_day": busiest_hour_of_day,
        "longest_focus_session": longest_focus_session,
        "first_capture_date": first_capture_date,
        "last_capture_date": last_capture_date,
        "total_active_days": total_active_days,
        "average_daily_shots": average_daily_shots,
        "longest_active_streak": longest_active_streak,
    }

    log.info(
        "yearly_wrapped.computed",
        year=year,
        total_shots=total_shots,
        total_active_days=total_active_days,
        total_unique_apps=total_unique_apps,
        total_notes=total_notes,
        voice_hours=total_voice_hours,
        longest_streak=longest_active_streak,
    )
    return payload


__all__ = ["compute_yearly_wrapped"]
