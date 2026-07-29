"""Supervised worker for owner-scoped graph and embedding projections."""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import time
from datetime import UTC, datetime
from typing import Final

from app.adapters.projection import (
    ExistingEmbeddingGateway,
    ExistingGraphGateway,
    SqliteProjectionOutbox,
)
from app.application.projection import ProjectionDispatcher
from app.auth.owner import get_owner_user_id
from app.domains.projection import ProjectionKind
from app.logging_setup import get_logger
from app.workers.heartbeat import beat

log = get_logger("persona.workers.memory_projection")
_MIN_POLL_SECONDS = 0.25
_MAX_POLL_SECONDS = 300.0
_IDLE_HEARTBEAT_SECONDS: Final[float] = 60.0


class _HeartbeatLimiter:
    """Write immediately on transitions and at most once/minute while idle."""

    def __init__(self) -> None:
        self._status = "idle"
        self._last_write_at = time.monotonic()

    async def emit(self, status: str) -> None:
        now = time.monotonic()
        if (
            status == self._status
            and now - self._last_write_at < _IDLE_HEARTBEAT_SECONDS
        ):
            return
        await beat("memory-projection", status)
        self._status = status
        self._last_write_at = now


async def run_memory_projection_worker(
    stop_event: asyncio.Event | None = None,
    *,
    poll_seconds: float = 5.0,
) -> None:
    """Project durable owner memory intents until shutdown."""

    if not _MIN_POLL_SECONDS <= poll_seconds <= _MAX_POLL_SECONDS:
        raise ValueError("poll_seconds must be in 0.25..300")
    stop = stop_event or asyncio.Event()
    lease_owner = (
        f"projection:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(6)}"
    )
    outbox = SqliteProjectionOutbox()
    dispatcher: ProjectionDispatcher | None = None
    configured_owner: int | None = None
    heartbeat = _HeartbeatLimiter()

    log.info("memory_projection.worker.started", poll_seconds=poll_seconds)
    while not stop.is_set():
        try:
            owner_id = await get_owner_user_id()
            if owner_id is None:
                dispatcher = None
                configured_owner = None
                did_work = False
                await heartbeat.emit("disabled:no_owner")
            else:
                owner = int(owner_id)
                if dispatcher is None or configured_owner != owner:
                    dispatcher = ProjectionDispatcher(
                        outbox,
                        {
                            ProjectionKind.GRAPH: ExistingGraphGateway(),
                            ProjectionKind.EMBEDDING: ExistingEmbeddingGateway(),
                        },
                        expected_owner_user_id=owner,
                        lease_owner=lease_owner,
                    )
                    configured_owner = owner
                did_work = await dispatcher.run_once(now=datetime.now(UTC))
                if did_work:
                    await heartbeat.emit("projected")
                else:
                    await heartbeat.emit("idle")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "memory_projection.worker.iteration_failed",
                error_type=type(exc).__name__,
            )
            did_work = False
            await heartbeat.emit("degraded")

        if did_work:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
    log.info("memory_projection.worker.stopped")


__all__ = ["run_memory_projection_worker"]
