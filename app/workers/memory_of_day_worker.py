"""Memory-of-the-day scheduler — pushes one anniversary highlight each morning.

Wakes up periodically; when the local-clock hour matches
``memory_of_day_hour_local`` (default ``9`` — 09:00, morning) it asks
:func:`app.memory_of_day.pick_memory` for one anniversary pick and, when
the picker returns data, forwards it to :func:`app.notifications.push`
so the bell widget surfaces the moment.

Wraps :class:`app.workers._bases.ClockScheduler` for the same once-per-day
guarantee shared by ai-reminders, daily-email, day-end-summary, etc. The
``kv_settings`` marker row is ``memory_of_day_last_fired``; a daemon that
restarts after firing this morning's push will not double-push.

Toggles
-------
* ``memory_of_day_enabled`` (kv) — ``"1"`` = on (the default).
* ``memory_of_day_hour_local`` (kv) — integer 0..23, default ``9``.

The picker is best-effort: when there is no data (empty corpus, fresh
install) it returns ``None`` and the scheduler advances the marker
anyway — re-firing on the next 30-min tick would just produce another
``None``, and the morning has already passed by tomorrow's tick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app import notifications
from app.logging_setup import get_logger
from app.memory_of_day import MemoryPick, pick_memory
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.memory_of_day")

_KV_HOUR: str = "memory_of_day_hour_local"
_KV_ENABLED: str = "memory_of_day_enabled"
_MARKER_KV: str = "memory_of_day_last_fired"

_DEFAULT_HOUR: int = 9  # morning — operator opens the laptop, sees a memory
_DEFAULT_ENABLED: bool = True
_POLL_INTERVAL_SECONDS: int = 1800  # 30 min, matching the other ClockScheduler users


async def _hour_getter() -> int:
    """Read the configured local-time hour; fall back to ``9``.

    A malformed value (non-int, out of 0..23) collapses to the default
    so a fat-finger in the settings UI can't park the scheduler
    permanently at hour 99.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("memory_of_day.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("memory_of_day.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """Return whether the scheduler should fire.

    Unlike the AI-reminders worker, this scheduler defaults to *enabled*
    — the feature is read-only, cheap (no LLM tokens), and the whole
    point is to delight the operator without them having to opt in.
    The literal string ``"1"`` is the explicit on; ``"0"`` (or any
    other value once written) is the explicit off; an absent row keeps
    the default.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return _DEFAULT_ENABLED
    return raw.strip() == "1"


def _build_title(pick: MemoryPick) -> str:
    """Compose the notification headline from the picked memory."""
    years = int(pick["years_back"])
    prefix = "1 год назад" if years == 1 else f"{years} года назад"
    kind = pick["kind"]
    if kind == "pinned_shot":
        return f"{prefix} — закреплённый момент"
    if kind == "daily_pin":
        return f"{prefix} — итог дня"
    return f"{prefix} — момент из этого дня"


def _build_link(pick: MemoryPick) -> str:
    """Pick the URL the notification should deep-link to.

    Shot-based picks (pinned, random) jump straight to the single-shot
    page; the daily-pin pick goes to the anniversary replay so the
    operator sees the day in context rather than just the one-liner.
    """
    kind = pick["kind"]
    if kind in {"pinned_shot", "random_shot"}:
        return f"/screenshot/{int(pick['shot_id'])}"
    return "/memory/replay"


async def _job_push_memory() -> None:
    """One scheduler tick — pick a memory and, if any, push a notification.

    Mirrors the swallow-and-log pattern used elsewhere in the workers
    package: any unexpected exception from the picker or from
    ``notifications.push`` is logged but not re-raised, because
    ``ClockScheduler`` would otherwise skip the per-day marker and we'd
    re-fire on the next 30-min tick.
    """
    log.info("memory_of_day.job.start")
    try:
        pick = await pick_memory()
    except Exception as exc:
        log.exception("memory_of_day.pick_failed", error=str(exc))
        return

    if pick is None:
        log.info("memory_of_day.job.no_memory")
        return

    title = _build_title(pick)
    body = pick.get("summary") or ""
    link = _build_link(pick)
    try:
        notif_id = await notifications.push(
            kind="memory-of-day",
            title=title,
            body=body or None,
            link=link,
            severity="info",
        )
    except Exception as exc:
        log.exception("memory_of_day.push_failed", error=str(exc))
        return

    log.info(
        "memory_of_day.job.done",
        notif_id=notif_id,
        kind=pick["kind"],
        years_back=int(pick["years_back"]),
        date_iso=pick["date_iso"],
    )


async def run_memory_of_day_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="memory-of-day",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_push_memory,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_memory_of_day_worker"]
