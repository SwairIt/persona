"""CLI entry point: ``python -m app.integrations.telegram``."""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress

from app.integrations.telegram.config import TelegramConfig
from app.integrations.telegram.repository import TelegramRepository
from app.integrations.telegram.worker import TelegramWorker
from app.settings import get_settings
from app.storage.db import init_database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the owner-only Persona Telegram worker.")
    parser.add_argument(
        "--rotate-pairing-code",
        action="store_true",
        help="Create and print a new one-time /claim code before starting.",
    )
    parser.add_argument(
        "--pairing-code-only",
        action="store_true",
        help="Create a one-time /claim code and exit.",
    )
    return parser


async def _main(args: argparse.Namespace) -> int:
    # A fresh standalone install needs the schema.  On a normal deployed
    # instance uvicorn already owns the existing database, so do not replay
    # all migrations from this worker.
    if not get_settings().db_path.exists():
        await init_database()
    repository = TelegramRepository()
    if args.rotate_pairing_code or args.pairing_code_only:
        code = await repository.create_pairing_code()
        print("Одноразовая команда привязки (не пересылай её):")
        print(f"/claim {code}")
        if args.pairing_code_only:
            return 0
    config = TelegramConfig.load()
    worker = TelegramWorker(config, repository=repository)
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, worker.stop)
    await worker.run()
    return 0


def main() -> int:
    return asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
