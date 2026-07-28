"""Lifecycle adapter for the owner-only Telegram long-poll worker."""

from __future__ import annotations

import asyncio

from app.integrations.telegram.config import TelegramConfig
from app.integrations.telegram.worker import TelegramWorker
from app.logging_setup import get_logger

log = get_logger("persona.workers.telegram")


async def run_telegram_worker() -> None:
    """Run Telegram inside the supervised application lifecycle.

    A self-host without a bot token keeps a dormant, cancellable task instead
    of entering the supervisor's failure/restart loop. Adding a token requires
    an application restart, just like every other `.env` setting.
    """
    config = TelegramConfig.load()
    if not config.bot_token:
        log.info("telegram.worker.disabled", reason="missing_bot_token")
        await asyncio.Event().wait()
        return

    worker = TelegramWorker(config)
    try:
        await worker.run()
    finally:
        worker.stop()


__all__ = ["run_telegram_worker"]
