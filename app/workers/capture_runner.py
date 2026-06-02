"""Standalone capture worker entry point — runs without the web UI.

Useful when the user wants to:
  - run only the capture loop on boot (headless)
  - debug the capture pipeline without uvicorn noise
  - capture from a machine while serving the UI from a different one (later, v1)
"""

from __future__ import annotations

import asyncio
import signal
from types import FrameType

from app.logging_setup import configure_logging, get_logger
from app.storage.db import init_database
from app.workers.capture_loop import run_capture_loop
from app.workers.control import get_controller
from app.workers.embeddings_worker import run_embeddings_worker
from app.workers.ocr_worker import run_ocr_worker
from app.workers.retention import run_retention_worker

log = get_logger("persona.runner")


def _install_signal_handlers(controller: object) -> None:
    def _handler(signum: int, frame: FrameType | None) -> None:
        log.info("runner.signal", signum=signum)
        controller.request_stop()  # type: ignore[attr-defined]

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


async def _amain() -> None:
    configure_logging()
    await init_database()
    controller = get_controller()
    controller.resume()
    _install_signal_handlers(controller)

    log.info("runner.starting")
    tasks = [
        asyncio.create_task(run_capture_loop(controller), name="capture-loop"),
        asyncio.create_task(run_ocr_worker(controller), name="ocr-worker"),
        asyncio.create_task(run_retention_worker(controller), name="retention-worker"),
        asyncio.create_task(run_embeddings_worker(controller), name="embeddings-worker"),
    ]
    await controller.stop_event.wait()
    log.info("runner.stopping")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("runner.stopped")


def main() -> None:
    """Console-script entry: `persona-capture`."""
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
