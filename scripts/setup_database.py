"""Initialise the Persona SQLite database. Idempotent — safe to re-run."""

from __future__ import annotations

import asyncio

from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings
from app.storage.db import init_database

log = get_logger("persona.setup")


async def main() -> None:
    configure_logging()
    settings = get_settings()
    settings.ensure_directories()
    log.info("setup.start", db_path=str(settings.db_path))
    await init_database()
    log.info("setup.done", db_path=str(settings.db_path))


if __name__ == "__main__":
    asyncio.run(main())
