"""Briefing scheduler — пуш одной короткой сводки активности раз в день.

Зеркало memory_of_day_worker: ClockScheduler с once-per-day гарантией. В
заданный локальный час (`briefing_hour_local`, деф. 9) собирает сводку
(app.briefing.build_briefing) из часовых карточек и пушит в колокольчик
(notifications.push, link → /chat). Toggle `briefing_enabled` (деф. вкл).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app import notifications
from app.briefing import build_briefing, build_briefing_cards, store_cards
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.quiet_hours import is_quiet_now
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.briefing")

_KV_HOUR = "briefing_hour_local"
_KV_ENABLED = "briefing_enabled"
_MARKER_KV = "briefing_last_fired"
_DEFAULT_HOUR = 9
_DEFAULT_ENABLED = True
_POLL_INTERVAL_SECONDS = 1800  # 30 мин — как у остальных ClockScheduler


async def _hour_getter() -> int:
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_HOUR
    return value if 0 <= value <= 23 else _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return _DEFAULT_ENABLED
    return raw.strip() == "1"


async def _job_push_briefing() -> None:
    log.info("briefing.job.start")
    # S3b — тихие часы: не тревожим проактивным пушем в quiet-hours окно.
    try:
        async with get_connection() as conn:
            if await is_quiet_now(conn):
                log.info("briefing.job.skipped_quiet")
                return
    except Exception as exc:  # noqa: BLE001 — тихие часы не должны ронять брифинг
        log.debug("briefing.quiet_check_failed", error=str(exc))

    # S3b — собрать и сохранить карточки (страница /briefing с фидбеком).
    cards_n = 0
    try:
        cards = await build_briefing_cards(when="morning")
        cards_n = await store_cards(cards, slot="morning")
    except Exception as exc:  # noqa: BLE001 — карточки best-effort
        log.debug("briefing.cards_failed", error=str(exc))

    try:
        result = await build_briefing(when="morning")
    except Exception as exc:  # noqa: BLE001
        log.exception("briefing.build_failed", error=str(exc))
        result = None
    if result is None and cards_n == 0:
        log.info("briefing.job.no_data")
        return
    title = result[0] if result else "🌅 Утренняя сводка"
    body = result[1] if result else None
    try:
        await notifications.push(
            kind="briefing",
            title=title,
            body=(f"{body}\n\n→ {cards_n} карточек на /briefing" if body else None),
            link="/briefing",
            severity="info",
        )
        log.info("briefing.job.done", cards=cards_n)
    except Exception as exc:  # noqa: BLE001
        log.exception("briefing.push_failed", error=str(exc))


async def run_briefing_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — drives a ClockScheduler."""
    scheduler = ClockScheduler(
        name="briefing",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_push_briefing,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_briefing_worker"]
