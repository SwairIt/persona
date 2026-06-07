"""Helper that pairs a ``kv_settings`` write with a sync event emission.

The plain :func:`app.storage.repository.set_kv` is used in dozens of
internal spots (worker watermarks, throttle level, last-run-at). Most
of those do NOT need to fan out to other devices — they're per-instance
state. This helper is the explicit opt-in for the small set of keys that
ARE user-facing settings the user expects to follow them across devices.

Use it in any settings POST handler that should sync:

    from app.sync.kv_hook import set_kv_and_emit
    ...
    await set_kv_and_emit(
        conn,
        key="theme",
        value="dark",
        user_id=session["user_id"],
        logical_clock=int(time.time() * 1000),
    )

The Lamport clock argument is the caller's responsibility — the simplest
right answer is a millisecond wall-clock timestamp. Two devices flipping
the same setting within the same millisecond is a corner case and the
last-write-wins tiebreak by event ``id`` keeps the resolution deterministic.
"""

from __future__ import annotations

import time
from typing import Any

import aiosqlite

from app.logging_setup import get_logger
from app.storage.repository import set_kv
from app.sync import append_event

log = get_logger("persona.sync.kv_hook")


def _wallclock_ms() -> int:
    """Default Lamport clock — wall-clock milliseconds."""
    return int(time.time() * 1000)


async def set_kv_and_emit(
    conn: aiosqlite.Connection,
    *,
    key: str,
    value: str,
    user_id: int,
    logical_clock: int | None = None,
    device_id: int | None = None,
) -> None:
    """Write to ``kv_settings`` and append a matching sync event.

    The write is canonical (it touches local DB immediately). The event
    emission is best-effort: if the event log write fails, the kv write
    still stands. This keeps user-facing settings responsive even when
    the sync log has an issue.
    """
    await set_kv(conn, key, value)
    clock = logical_clock if logical_clock is not None else _wallclock_ms()
    try:
        await append_event(
            user_id=user_id,
            kind="kv",
            op="update",
            payload={"key": key, "value": value},
            device_id=device_id,
            logical_clock=clock,
        )
    except Exception as exc:
        log.warning(
            "sync.kv_hook.event_emit_failed",
            key=key,
            error=str(exc),
        )


# Whitelist of kv keys that are settings the user expects to follow them
# across devices. Other callers that touch kv_settings (workers writing
# watermarks, throttle level, etc.) keep using the plain ``set_kv``.
SYNCABLE_KV_KEYS: frozenset[str] = frozenset(
    {
        "theme",
        "capture_interval_seconds_live",
        "capture_screens_disabled",
        "audio_capture_paused_live",
        "meeting_pause_enabled",
        "ui_language",
        "compact_mode",
        "grayscale_mode",
        "reduce_motion",
    }
)


async def maybe_emit_kv(
    *,
    key: str,
    value: Any,
    user_id: int,
    logical_clock: int | None = None,
    device_id: int | None = None,
) -> bool:
    """Emit a kv sync event ONLY when ``key`` is in the whitelist.

    Convenience for sites that already wrote to ``kv_settings`` via the
    plain ``set_kv`` and only want to opt-in to sync for known keys.
    Returns ``True`` when an event was appended.
    """
    if key not in SYNCABLE_KV_KEYS:
        return False
    clock = logical_clock if logical_clock is not None else _wallclock_ms()
    try:
        await append_event(
            user_id=user_id,
            kind="kv",
            op="update",
            payload={"key": key, "value": str(value)},
            device_id=device_id,
            logical_clock=clock,
        )
        return True
    except Exception as exc:
        log.warning(
            "sync.kv_hook.event_emit_failed",
            key=key,
            error=str(exc),
        )
        return False
