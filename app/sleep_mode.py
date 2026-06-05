"""Sleep-mode auto-detector for the capture loop (v1.62).

When the user walks away from the desk for a while we already drop into
adaptive cadence (longer sleeps between shots), but adaptive cadence is
still *running* — it polls every minute, takes a screenshot of the same
idle desktop, and writes a row to ``screenshots``. For real away-from-
keyboard stretches (lunch, meetings on a different machine, sleep) that
is still a needless write amplifier.

Sleep mode is the lazier sibling of meeting-pause: it watches the
``seconds_since_last_input`` counter and, once it crosses a configurable
threshold (default 15 minutes), short-circuits the iteration the same
way meeting-pause does. The moment the user touches the keyboard or
mouse the counter resets, the next iteration sees ``sleeping=False``,
and capture resumes.

Two kv knobs:

* ``sleep_mode_enabled``           — ``"1"`` to arm the detector.
                                     Default ``"0"`` (opt-in).
* ``sleep_mode_threshold_minutes`` — integer minutes (default
                                     :data:`IDLE_THRESHOLD_MINUTES_DEFAULT`).

Transitions (``sleep`` / ``wake``) are persisted to
``sleep_mode_event`` so the settings page can render a recent-events
timeline. The capture loop holds a module-level ``_last_sleep_state``
cache so we never spam the event table on the steady-state branch —
only the edges produce rows.

All DB writes use parametrised SQL; failures never raise into the
capture loop (a broken audit log must not silently halt the loop).
"""

from __future__ import annotations

from typing import TypedDict

from app.capture import seconds_since_last_input
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.sleep_mode")


# Default idle threshold in minutes when the operator has not picked one.
# 15 minutes matches the conventional Windows screen-lock default — a
# stretch the user is "almost certainly not at the desk" rather than
# "just thinking with the keyboard at rest".
IDLE_THRESHOLD_MINUTES_DEFAULT: int = 15

# kv-key names. Kept module-level so the settings route and the worker
# import the same canonical strings rather than each spelling its own.
KV_ENABLED: str = "sleep_mode_enabled"
KV_THRESHOLD: str = "sleep_mode_threshold_minutes"

# Minimum/maximum threshold accepted from the form. Below 1 minute would
# put the loop into a sleep/wake flapping state every other tick; above
# 240 minutes is "you have effectively disabled capture" — clamp so the
# UI cannot foot-gun.
_MIN_THRESHOLD_MINUTES: int = 1
_MAX_THRESHOLD_MINUTES: int = 240


class SleepDecision(TypedDict, total=False):
    """Return value of :func:`should_sleep`.

    ``sleeping`` is always present. ``reason`` is set only when the
    feature is disabled (so the caller can branch on it without a kv
    re-read). ``idle_seconds`` and ``threshold_seconds`` are set on
    every enabled path so the caller can persist them on a transition.
    """

    sleeping: bool
    reason: str
    idle_seconds: float
    threshold_seconds: float


def _coerce_threshold_minutes(raw: str | None) -> int:
    """Parse and clamp the kv threshold value.

    Garbage (``None``, empty, non-int) collapses to the documented
    default. Out-of-range values clamp into ``[_MIN, _MAX]`` rather than
    raising — a broken kv row must not stop the loop, and an over-
    aggressive value is at worst a slow-capture annoyance.
    """
    if raw is None:
        return IDLE_THRESHOLD_MINUTES_DEFAULT
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return IDLE_THRESHOLD_MINUTES_DEFAULT
    if value < _MIN_THRESHOLD_MINUTES:
        return _MIN_THRESHOLD_MINUTES
    if value > _MAX_THRESHOLD_MINUTES:
        return _MAX_THRESHOLD_MINUTES
    return value


async def _read_settings() -> tuple[bool, int]:
    """Read the enabled flag and the threshold minutes from kv.

    Combined into a single helper so the capture-loop hot path makes
    one connection round-trip per iteration instead of two. Failures
    downgrade to ``(False, default)`` so a broken kv layer disables the
    feature rather than crashing the loop.
    """
    try:
        async with get_connection() as conn:
            enabled_raw = await get_kv(conn, KV_ENABLED)
            threshold_raw = await get_kv(conn, KV_THRESHOLD)
    except Exception as exc:
        log.debug("sleep_mode.kv_read_failed", error=str(exc))
        return (False, IDLE_THRESHOLD_MINUTES_DEFAULT)
    enabled = (enabled_raw or "0").strip() == "1"
    threshold = _coerce_threshold_minutes(threshold_raw)
    return (enabled, threshold)


async def should_sleep(
    idle_seconds: float | None = None,
) -> SleepDecision:
    """Decide whether the capture loop should skip this iteration.

    When ``idle_seconds`` is ``None`` we sample it via
    :func:`app.capture.seconds_since_last_input` ourselves — useful for
    callers that don't already have the value (the settings page's
    "current status" widget, for example). Callers on the hot path
    pass in the value they already sampled to avoid a redundant
    GetLastInputInfo syscall.

    Return shape:

    * ``{"sleeping": False, "reason": "disabled"}`` — kv flag is off.
    * ``{"sleeping": True, "idle_seconds": .., "threshold_seconds": ..}``
      — flag is on AND idle exceeded threshold.
    * ``{"sleeping": False, "idle_seconds": .., "threshold_seconds": ..}``
      — flag is on but idle is below threshold.
    """
    enabled, threshold_minutes = await _read_settings()
    if not enabled:
        return {"sleeping": False, "reason": "disabled"}

    observed = (
        float(idle_seconds)
        if idle_seconds is not None
        else float(seconds_since_last_input())
    )
    threshold_seconds = float(threshold_minutes) * 60.0

    if observed > threshold_seconds:
        return {
            "sleeping": True,
            "idle_seconds": observed,
            "threshold_seconds": threshold_seconds,
        }
    return {
        "sleeping": False,
        "idle_seconds": observed,
        "threshold_seconds": threshold_seconds,
    }


async def record_state_change(state: str, idle_seconds: float) -> None:
    """Persist a single sleep/wake transition.

    ``state`` MUST be ``"sleep"`` or ``"wake"`` to satisfy the CHECK
    constraint on ``sleep_mode_event.state``; the helper validates the
    value up front so a bad caller fails fast instead of triggering a
    SQLite constraint error inside the worker.

    Failure modes (DB locked, transient I/O) are swallowed and logged at
    DEBUG — a broken audit log must not silently halt the capture loop.
    """
    if state not in {"sleep", "wake"}:
        log.warning("sleep_mode.bad_state", state=state)
        return
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO sleep_mode_event (state, idle_seconds) "
                "VALUES (?, ?)",
                (state, int(idle_seconds)),
            )
            await conn.commit()
    except Exception as exc:
        log.debug("sleep_mode.record_failed", error=str(exc))
        return
    log.info(
        "sleep_mode.state_change",
        state=state,
        idle_seconds=int(idle_seconds),
    )


async def recent_events(limit: int = 50) -> list[dict[str, object]]:
    """Return the most recent ``limit`` rows for the settings page.

    Newest first. Each row is shaped ``{id, occurred_at, state,
    idle_seconds}`` so the template and the JSON endpoint share a
    single payload schema. The helper is intentionally read-only; the
    audit trail is append-only by policy and there is no admin path
    that mutates rows.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, occurred_at, state, idle_seconds "
                "FROM sleep_mode_event "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            rows = await cursor.fetchall()
    except Exception as exc:
        log.debug("sleep_mode.events_read_failed", error=str(exc))
        return []
    return [
        {
            "id": int(row["id"]),
            "occurred_at": str(row["occurred_at"]),
            "state": str(row["state"]),
            "idle_seconds": int(row["idle_seconds"]),
        }
        for row in rows
    ]


__all__ = [
    "IDLE_THRESHOLD_MINUTES_DEFAULT",
    "KV_ENABLED",
    "KV_THRESHOLD",
    "SleepDecision",
    "recent_events",
    "record_state_change",
    "should_sleep",
]
