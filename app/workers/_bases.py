"""Worker base classes (v1.26).

Two recurring patterns made up ~1200 LOC of nearly-identical code across
12 worker files. This module collapses them into two reusable shapes:

* :class:`BackfillRunner` — periodic poll, build missing rows.
  Used by hourly_card_worker, weekly_card_worker, daily_pin_worker,
  card_enrichment_worker.

* :class:`ClockScheduler` — wake every poll_interval, fire when the local
  clock hits hour_local. Idempotent via a kv marker row keyed by date.
  Used by digest_scheduler, weekly_digest_scheduler,
  monthly_digest_scheduler, daily_email_scheduler,
  weekly_stats_email_scheduler, day_end_summary_scheduler,
  auto_backup_scheduler.

Each existing worker becomes a 20-line wrapper that instantiates a base
+ provides the callback. The legacy `run_<name>_worker()` entry point
stays as a thin alias so the lifespan task list in app/web/main.py
doesn't have to change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from app.logging_setup import get_logger
from app.workers.heartbeat import beat

log = get_logger("persona.workers.base")


# ---------------------------------------------------------------------------
# BackfillRunner — poll, build missing rows, sleep
# ---------------------------------------------------------------------------


class BackfillRunner:
    """Loop that fills missing rows from a producer callback.

    Parameters
    ----------
    name:
        Slug used for heartbeat + log lines (e.g. ``"hourly-card-worker"``).
    poll_seconds:
        How often to wake up between scans.
    list_missing:
        Async callable returning the list of keys (anything hashable +
        loggable) that still need work. Typically a SQL ``SELECT … WHERE
        … NOT IN(SELECT … FROM target)`` style query.
    build_one:
        Async callable invoked once per missing key. Should be
        idempotent — the runner does not deduplicate across ticks.

    The runner traps individual ``build_one`` failures so one bad row
    doesn't kill the loop. Cancellation propagates as usual.
    """

    def __init__(
        self,
        *,
        name: str,
        poll_seconds: int,
        list_missing: Callable[[], Awaitable[list[Any]]],
        build_one: Callable[[Any], Awaitable[Any]],
    ) -> None:
        self._name = name
        self._poll_seconds = poll_seconds
        self._list_missing = list_missing
        self._build_one = build_one

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        log.info("worker.started", worker=self._name, poll_seconds=self._poll_seconds)

        while not stop.is_set():
            await beat(self._name)
            try:
                missing = await self._list_missing()
                built = 0
                for key in missing:
                    try:
                        result = await self._build_one(key)
                        if result is not None:
                            built += 1
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "worker.build_failed",
                            worker=self._name,
                            key=str(key)[:200],
                            error=str(exc),
                        )
                if built:
                    log.info("worker.cycle", worker=self._name, built=built)
            except asyncio.CancelledError:
                log.info("worker.cancelled", worker=self._name)
                raise
            except Exception as exc:
                log.exception(
                    "worker.iteration_failed", worker=self._name, error=str(exc)
                )

            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

        log.info("worker.stopped", worker=self._name)


# ---------------------------------------------------------------------------
# ClockScheduler — wake hourly, fire when wall-clock hits hour_local, mark idempotent
# ---------------------------------------------------------------------------


class ClockScheduler:
    """Wake every poll_seconds, fire ``job`` when local hour matches.

    Uses a per-date kv marker so a daemon that restarts after running
    today's job doesn't fire it again. Markers are stored as
    ``<marker_kv>=<YYYY-MM-DD>``; reading a different date triggers the
    job and updates the marker.

    Parameters
    ----------
    name:
        Slug for logs + heartbeat.
    poll_seconds:
        Wake cadence. 1800 (30 min) is enough — we only need to catch
        the configured hour at some point during the day.
    hour_local_getter:
        Async callable returning the configured local-time hour (0..23)
        the job should fire at. Wrapping in a callable lets the operator
        change it via UI without restarting the daemon.
    weekday_getter:
        Optional async callable returning the weekday integer
        (0=Monday..6=Sunday) the job should fire on. ``None`` →
        every day.
    enabled_getter:
        Async callable returning whether the job is currently enabled.
        Disabled jobs log a single startup line and idle for the rest
        of the process lifetime — they still consult ``enabled_getter``
        each tick so toggling at runtime takes effect.
    marker_kv:
        The kv_settings key under which the "last fired date" is stored.
    job:
        Async callable; runs the actual work (e.g. send daily email).
    """

    def __init__(
        self,
        *,
        name: str,
        hour_local_getter: Callable[[], Awaitable[int]],
        enabled_getter: Callable[[], Awaitable[bool]],
        marker_kv: str,
        job: Callable[[], Awaitable[Any]],
        poll_seconds: int = 1800,
        weekday_getter: Callable[[], Awaitable[int | None]] | None = None,
    ) -> None:
        self._name = name
        self._hour_local_getter = hour_local_getter
        self._enabled_getter = enabled_getter
        self._marker_kv = marker_kv
        self._job = job
        self._poll_seconds = poll_seconds
        self._weekday_getter = weekday_getter

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        stop = stop_event or asyncio.Event()
        log.info(
            "scheduler.started",
            scheduler=self._name,
            poll_seconds=self._poll_seconds,
        )

        # The kv helpers are imported lazily so an unrelated startup
        # failure (e.g. broken kv_settings schema) doesn't break import.
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv, set_kv  # noqa: PLC0415

        while not stop.is_set():
            await beat(self._name)
            try:
                enabled = await self._enabled_getter()
                if not enabled:
                    log.debug("scheduler.disabled", scheduler=self._name)
                else:
                    target_hour = await self._hour_local_getter()
                    target_weekday = (
                        await self._weekday_getter() if self._weekday_getter else None
                    )
                    now = datetime.now().astimezone()
                    today_iso = now.date().isoformat()

                    weekday_ok = target_weekday is None or now.weekday() == target_weekday
                    hour_ok = now.hour == target_hour

                    if weekday_ok and hour_ok:
                        async with get_connection() as conn:
                            last_fired = await get_kv(conn, self._marker_kv)
                        if (last_fired or "").strip() != today_iso:
                            try:
                                await self._job()
                                async with get_connection() as conn:
                                    await set_kv(conn, self._marker_kv, today_iso)
                                log.info(
                                    "scheduler.fired",
                                    scheduler=self._name,
                                    date=today_iso,
                                )
                            except Exception as exc:
                                log.exception(
                                    "scheduler.job_failed",
                                    scheduler=self._name,
                                    error=str(exc),
                                )
            except asyncio.CancelledError:
                log.info("scheduler.cancelled", scheduler=self._name)
                raise
            except Exception as exc:
                log.exception(
                    "scheduler.iteration_failed",
                    scheduler=self._name,
                    error=str(exc),
                )

            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                continue

        log.info("scheduler.stopped", scheduler=self._name)


# Helpers used by ``date.fromisoformat`` callers — exposed so callers can
# guard their own marker-read parsing if they want.
def _parse_date_safe(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


__all__ = ["BackfillRunner", "ClockScheduler"]
