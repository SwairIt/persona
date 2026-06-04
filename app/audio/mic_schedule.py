"""Mic recording schedule (v1.17).

Returns ``True`` when the current local weekday + hour fall inside the
configured window. The audio worker calls this before each iteration
and skips ``record_chunk`` when ``False``.

The schedule lives in ``kv_settings`` so the UI can edit it live
without a daemon restart. The hot path here is the worker loop, so
every read parses the small kv strings into a tiny in-memory state on
each call — no cache layer needed (audio worker iterates ~once per
~30 s, dwarfing the few SQL reads).
"""

from __future__ import annotations

from datetime import datetime

import aiosqlite

from app.logging_setup import get_logger
from app.storage.repository import get_kv

log = get_logger("persona.audio.mic_schedule")

_WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


async def is_recording_allowed_now(
    conn: aiosqlite.Connection,
    now: datetime | None = None,
) -> bool:
    """Return True if the schedule (or its absence) permits recording now."""
    enabled = (await get_kv(conn, "mic_schedule_enabled") or "0").strip()
    if enabled != "1":
        return True

    days_raw = (await get_kv(conn, "mic_schedule_days") or "").strip().lower()
    days = {d.strip() for d in days_raw.split(",") if d.strip() in _WEEKDAY_CODES}
    if not days:
        # Misconfigured → fall back to "allow"; the user opted in to a
        # schedule but didn't pick any day. We don't want a silent
        # blanket-block from a typo.
        log.warning("mic_schedule.no_days_configured")
        return True

    try:
        start_hour = max(0, min(24, int(await get_kv(conn, "mic_schedule_start_hour") or "0")))
        end_hour = max(0, min(24, int(await get_kv(conn, "mic_schedule_end_hour") or "24")))
    except ValueError:
        log.warning("mic_schedule.bad_hour_value")
        return True

    if start_hour >= end_hour:
        # User asked for "from X to Y" where Y <= X — interpret as the
        # window wraps midnight (e.g. 22 → 06 means "10 PM through 6 AM").
        # If they really meant "block everything" they'd set the master
        # mic toggle, not a schedule.
        return _is_in_wrap_window(
            now=now or datetime.now().astimezone(),
            days=days,
            start_hour=start_hour,
            end_hour=end_hour,
        )

    moment = now or datetime.now().astimezone()
    code = _WEEKDAY_CODES[moment.weekday()]
    if code not in days:
        return False
    return start_hour <= moment.hour < end_hour


def _is_in_wrap_window(
    now: datetime,
    days: set[str],
    start_hour: int,
    end_hour: int,
) -> bool:
    """Handle the "22 → 06" overnight case across two calendar days."""
    code = _WEEKDAY_CODES[now.weekday()]
    if now.hour >= start_hour:
        # Late-evening half — today's weekday must be enabled.
        return code in days
    if now.hour < end_hour:
        # Early-morning half — *yesterday's* weekday is the one the user
        # actually picked (their Mon-night roll-over).
        prev = _WEEKDAY_CODES[(now.weekday() - 1) % 7]
        return prev in days
    return False


__all__ = ["is_recording_allowed_now"]
