"""Idle-vs-active dashboard — per-day breakdown of how much of the user's
attended time was actually *active* vs spent AFK (away-from-keyboard).

Definition of *active* / *idle*:

    Every screenshot row carries an ``idle_seconds`` value (Windows
    ``GetLastInputInfo``).  A shot whose ``idle_seconds < idle_threshold_s``
    is considered **active**; ``>=`` the threshold is **idle**.

Definition of *time spent*:

    Walk the day's shots in chronological order.  For each *adjacent pair*
    ``(prev, curr)`` whose wall gap is ``<= max_gap_s``, attribute the gap
    to a bucket based on the *latter* shot's classification — that shot is
    the freshest evidence of what the user was doing at the moment.  Wider
    gaps (or the first shot of the day) contribute nothing — we treat them
    as "capture paused" rather than guessing.

This is intentionally a thin, dict-returning surface that mirrors the
shape of :mod:`app.time_on_app`; it can be consumed by both the HTML
dashboard and the JSON API without further reshaping.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.idle")

DEFAULT_IDLE_THRESHOLD_S = 60
DEFAULT_MAX_GAP_S = 300


class IdleStats(TypedDict):
    day: str
    active_seconds: int
    idle_seconds: int
    active_shots: int
    idle_shots: int
    first_capture: str | None
    last_capture: str | None


def _walk_rows(
    rows: list[tuple[str, float | None]],
    idle_threshold_s: int,
    max_gap_s: int,
) -> tuple[int, int, int, int]:
    """Fold ``[(captured_at_iso, idle_seconds), ...]`` into bucket totals.

    Rows are assumed already ordered by ``captured_at`` ASC. Returns
    ``(active_seconds, idle_seconds, active_shots, idle_shots)``.
    """
    active_secs = 0
    idle_secs = 0
    active_shots = 0
    idle_shots = 0
    prev_dt: datetime | None = None

    for captured_at_raw, idle_raw in rows:
        when = datetime.fromisoformat(str(captured_at_raw))
        # NULL idle_seconds is treated as "active" — best-effort, matches
        # the capture-loop invariant that we only persist shots whose idle
        # time was below the *capture* threshold at sample time.
        idle_value = float(idle_raw) if idle_raw is not None else 0.0
        is_idle = idle_value >= idle_threshold_s

        if is_idle:
            idle_shots += 1
        else:
            active_shots += 1

        if prev_dt is not None:
            diff = (when - prev_dt).total_seconds()
            if 0 < diff <= max_gap_s:
                if is_idle:
                    idle_secs += int(diff)
                else:
                    active_secs += int(diff)
        prev_dt = when

    return active_secs, idle_secs, active_shots, idle_shots


async def daily_idle(
    day_iso: str,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
    max_gap_s: int = DEFAULT_MAX_GAP_S,
) -> IdleStats:
    """Return active-vs-idle stats for the given local day.

    ``day_iso`` is a ``YYYY-MM-DD`` string. Unknown / malformed values
    fall back to today (matching the convention used by sibling stats
    modules).
    """
    try:
        target = date.fromisoformat(day_iso)
    except ValueError:
        log.warning("idle.bad_day_iso", day_iso=day_iso)
        target = datetime.now().astimezone().date()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT captured_at, idle_seconds FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "ORDER BY captured_at",
            (target.isoformat(),),
        )
        raw_rows = list(await cursor.fetchall())

    rows: list[tuple[str, float | None]] = [
        (str(r["captured_at"]), r["idle_seconds"]) for r in raw_rows
    ]

    active_secs, idle_secs, active_shots, idle_shots = _walk_rows(
        rows,
        idle_threshold_s=idle_threshold_s,
        max_gap_s=max_gap_s,
    )

    first_capture = str(raw_rows[0]["captured_at"]) if raw_rows else None
    last_capture = str(raw_rows[-1]["captured_at"]) if raw_rows else None

    result: IdleStats = {
        "day": target.isoformat(),
        "active_seconds": active_secs,
        "idle_seconds": idle_secs,
        "active_shots": active_shots,
        "idle_shots": idle_shots,
        "first_capture": first_capture,
        "last_capture": last_capture,
    }

    log.info(
        "idle.computed",
        day=target.isoformat(),
        active_seconds=active_secs,
        idle_seconds=idle_secs,
        active_shots=active_shots,
        idle_shots=idle_shots,
        idle_threshold_s=idle_threshold_s,
        max_gap_s=max_gap_s,
    )
    return result
