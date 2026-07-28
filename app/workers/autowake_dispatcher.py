"""Durable proactive-message dispatcher.

Integration is explicit: the composition root must provide an
``OwnerTelegramGateway`` that resolves only the configured owner's private
chat.  This module never accepts a Telegram chat id.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
from datetime import datetime

from app.adapters.autowake import SqliteAutowakeRepository, TelegramOwnerGateway
from app.application.autowake import (
    AutowakeDispatcher,
    OwnerTelegramGateway,
)
from app.auth.owner import get_owner_user_id
from app.domains.autowake import AutowakePolicy
from app.integrations.telegram.api import TelegramBotAPI
from app.integrations.telegram.config import TelegramConfig
from app.integrations.telegram.repository import TelegramRepository
from app.logging_setup import get_logger
from app.workers.heartbeat import beat

log = get_logger("persona.workers.autowake")


async def run_owner_autowake_dispatcher() -> None:
    """Compose the durable dispatcher with the configured owner Telegram DM."""

    config = TelegramConfig.load()
    if not config.bot_token:
        log.info("autowake.worker.disabled", reason="missing_bot_token")
        await asyncio.Event().wait()
        return
    owner_id = await get_owner_user_id()
    if owner_id is None:
        raise RuntimeError("Persona owner account is not configured")

    repository = TelegramRepository()
    gateway = TelegramOwnerGateway(
        TelegramBotAPI(config.bot_token),
        repository,
        expected_owner_user_id=owner_id,
        configured_telegram_user_id=config.owner_telegram_user_id,
    )
    lease_owner = (
        f"autowake:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(6)}"
    )
    await run_autowake_dispatcher(
        gateway,
        lease_owner=lease_owner,
        expected_owner_user_id=owner_id,
    )


async def run_autowake_dispatcher(
    gateway: OwnerTelegramGateway,
    stop_event: asyncio.Event | None = None,
    *,
    lease_owner: str = "persona-autowake",
    expected_owner_user_id: int,
    poll_seconds: float = 5.0,
) -> None:
    """Poll and deliver one owner message at a time until stopped."""

    if not 0.25 <= poll_seconds <= 300:
        raise ValueError("poll_seconds must be in 0.25..300")
    stop = stop_event or asyncio.Event()
    dispatcher = AutowakeDispatcher(
        SqliteAutowakeRepository(),
        gateway,
        policy=AutowakePolicy(),
        lease_owner=lease_owner,
        expected_owner_user_id=expected_owner_user_id,
    )
    log.info("autowake.worker.started", poll_seconds=poll_seconds)
    while not stop.is_set():
        await beat("autowake-dispatcher")
        try:
            did_work = await dispatcher.run_once(now=datetime.now().astimezone())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log transport exception text: it can contain bot tokens,
            # proxy credentials, or message content.
            log.error(
                "autowake.worker.iteration_failed",
                error_type=type(exc).__name__,
            )
            did_work = False
        if did_work:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
    log.info("autowake.worker.stopped")


__all__ = ["run_autowake_dispatcher", "run_owner_autowake_dispatcher"]
