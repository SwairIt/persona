"""Supervised producer for silent-by-default proactive Telegram messages."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.adapters.autowake.impulses import (
    LLMImpulseDecisionAdapter,
    TelegramImpulseContextAdapter,
)
from app.adapters.autowake.sqlite_repository import SqliteAutowakeRepository
from app.application.autowake import AutowakeService, PersonaImpulseProducer
from app.auth.owner import get_owner_user_id
from app.domains.autowake import AutowakePolicy, AutowakePolicyConfig
from app.integrations.telegram.config import TelegramConfig
from app.integrations.telegram.repository import TelegramRepository
from app.logging_setup import get_logger
from app.thinking.settings import load_thinking_settings
from app.workers.heartbeat import beat

if TYPE_CHECKING:
    from app.application.autowake.impulses import PersonaImpulseProducer as Producer

log = get_logger("persona.workers.impulse")


async def run_persona_impulse_worker() -> None:
    """Compose the producer only when durable Telegram delivery is available."""

    config = TelegramConfig.load()
    if not config.bot_token:
        log.info("persona.impulse.disabled", reason="missing_bot_token")
        await asyncio.Event().wait()
        return
    owner_id = await get_owner_user_id()
    if owner_id is None:
        raise RuntimeError("Persona owner account is not configured")

    # Owner-tunable (2026-07-31): these used to be hardcoded here, invisible
    # to the "how autonomous is she" controls on /settings/thinking. Defaults
    # (30 min cooldown, 12/day) are unchanged — see app.thinking.settings.
    thinking_settings = await load_thinking_settings()
    policy = AutowakePolicy(
        AutowakePolicyConfig(
            cooldown=timedelta(minutes=thinking_settings.impulse_cooldown_minutes),
            daily_cap=thinking_settings.impulse_daily_cap,
        )
    )
    repository = SqliteAutowakeRepository()
    producer = PersonaImpulseProducer(
        repository,
        AutowakeService(
            repository,
            expected_owner_user_id=owner_id,
            policy=policy,
        ),
        TelegramImpulseContextAdapter(
            TelegramRepository(),
            configured_allowed_chat_ids=config.allowed_chat_ids,
        ),
        LLMImpulseDecisionAdapter(),
        owner_user_id=owner_id,
        policy=policy,
    )
    await run_persona_impulse_producer(producer)


async def run_persona_impulse_producer(
    producer: Producer,
    stop_event: asyncio.Event | None = None,
    *,
    cadence_seconds: float = 300.0,
) -> None:
    """Run one bounded attempt per cadence; transient LLM failures retry later."""

    if not 30 <= cadence_seconds <= 3600:
        raise ValueError("cadence_seconds must be in 30..3600")
    stop = stop_event or asyncio.Event()
    log.info("persona.impulse.started", cadence_seconds=cadence_seconds)
    while not stop.is_set():
        await beat("persona-impulse-producer")
        try:
            outcome = await producer.run_once(now=datetime.now().astimezone())
            log.info(
                "persona.impulse.iteration",
                emitted=outcome.emitted,
                reason=outcome.reason,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Model/transport exception text may contain URLs or provider data.
            # No failure marker is persisted: the next cadence retries cleanly.
            log.warning(
                "persona.impulse.iteration_failed",
                error_type=type(exc).__name__,
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=cadence_seconds)
        except TimeoutError:
            continue
    log.info("persona.impulse.stopped")


__all__ = ["run_persona_impulse_producer", "run_persona_impulse_worker"]
