"""Ночная консолидация памяти («сон»), Hermes-style — ``ClockScheduler``-обёртка.

docs/MEMORY_RESEARCH.md §3. Раз в сутки (kv ``dream_hour_local``, дефолт ``3`` —
03:00), **OPT-IN** (kv ``dream_enabled``, дефолт ``"0"``). Per-date
идемпотентность — kv-маркер ``dream_last_fired`` (как у всех ``ClockScheduler``:
memory-of-day, ai-reminders, day-end-summary). Сам цикл (3 фазы сна) —
:func:`app.chat.reflection.run_dream_cycle` для владельца аккаунта
(:func:`app.auth.owner.get_owner_user_id`).

Гейт активности и durable retry живут внутри цикла. Статусы ``quiet`` и
``retry`` поднимают :class:`_DreamDeferred`, чтобы ``ClockScheduler`` не
проставил per-date маркер и попробовал снова на следующем 30-минутном тике.

Toggles
-------
* ``dream_enabled`` (kv) — ``"1"`` = on; всё прочее (в т.ч. отсутствие) = off.
* ``dream_hour_local`` (kv) — целое 0..23, дефолт ``3``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.auth.owner import get_owner_user_id
from app.chat.reflection import run_dream_cycle
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.dream")

_KV_HOUR: str = "dream_hour_local"
_KV_ENABLED: str = "dream_enabled"
_MARKER_KV: str = "dream_last_fired"

_DEFAULT_HOUR: int = 3  # 03:00 — ночь, низкая активность, модель не на hot-path
_POLL_INTERVAL_SECONDS: int = 1800  # 30 мин, как у остальных ClockScheduler


class _DreamDeferred(Exception):
    """Подняли, чтобы ``ClockScheduler`` НЕ проставил per-date маркер и
    попробовал снова на следующем тике (гейт активности ``quiet_minutes``)."""


async def _hour_getter() -> int:
    """Локальный час запуска; битое/вне-диапазона значение → дефолт ``3``."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("dream.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("dream.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """OPT-IN: только литерал ``"1"`` = включено; отсутствие/прочее = выключено."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def _job_run_dream() -> None:
    """Один прогон «сна» для владельца. Тихо пропускает, если владельца нет.

    На ``quiet``/``retry`` поднимает :class:`_DreamDeferred`, чтобы маркер не
    проставился и цикл повторился на следующем тике. Прочие статусы завершают
    текущую календарную попытку.
    """
    owner = await get_owner_user_id()
    if owner is None:
        log.info("dream.job.no_owner")
        return
    result = await run_dream_cycle(owner)
    status = (result or {}).get("status")
    if status in {"quiet", "retry"}:
        log.info("dream.job.deferred", user_id=owner, reason=status)
        raise _DreamDeferred(f"dream cycle deferred ({status}); retry next tick")
    log.info(
        "dream.job.done",
        user_id=owner,
        status=status,
        candidates=(result or {}).get("candidates"),
        promoted=(result or {}).get("promoted"),
        dream=(result or {}).get("dream"),
    )


async def run_dream_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="dream",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_run_dream,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_dream_worker"]
