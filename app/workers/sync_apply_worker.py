"""Background worker that drains the sync_event pending queue.

Polls every 30 seconds. For each user with at least one pending event,
calls ``apply_pending`` which materialises ``note`` and ``kv`` events
into ``notes`` and ``kv_settings``.

The worker is intentionally CPU-cheap; the actual work is small writes
inside ``apply_pending``. We keep the poll interval short so a push from
a remote device gets materialised within a half-minute, not hours.
"""

from __future__ import annotations

import asyncio

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.sync import apply_pending
from app.workers.heartbeat import beat

log = get_logger("persona.sync_apply_worker")

_POLL_SECONDS = 30.0


async def _users_with_pending() -> list[int]:
    """Return user_ids that have at least one ``applied_at IS NULL`` event."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT user_id FROM sync_event "
            "WHERE applied_at IS NULL"
        )
        rows = await cursor.fetchall()
    return [int(r["user_id"]) for r in rows]


async def run_sync_apply_worker() -> None:
    """Lifespan entry-point. Polls forever until cancelled."""
    log.info("sync_apply_worker.started", poll_seconds=_POLL_SECONDS)
    while True:
        try:
            await beat("sync-apply-worker")
            users = await _users_with_pending()
            for user_id in users:
                try:
                    result = await apply_pending(user_id)
                    if result.get("applied", 0) or result.get("failed", 0):
                        log.info(
                            "sync_apply_worker.tick",
                            user_id=user_id,
                            **result,
                        )
                except Exception as exc:
                    log.warning(
                        "sync_apply_worker.user_failed",
                        user_id=user_id,
                        error=str(exc),
                    )
        except Exception as exc:
            log.warning("sync_apply_worker.tick_failed", error=str(exc))
        await asyncio.sleep(_POLL_SECONDS)
