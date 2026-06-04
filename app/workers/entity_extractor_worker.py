"""Entity extractor worker (v1.27).

Periodic driver for :func:`app.entity_extractor.ingest_mentions_from_hourly_cards`.

Sits on top of :class:`app.workers._bases.BackfillRunner` like the
auto-pin worker: the missing-list returns a single sentinel iff the
``entity_extraction_enabled`` kv flag is truthy, and ``build_one``
calls into the extractor exactly once per tick.

Cadence is 1800 s (30 min). Hourly cards arrive at most once an hour,
and a missed tick is harmless because the watermark kv guarantees
exactly-once consumption.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.entity_extractor import ingest_mentions_from_hourly_cards
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.entity_extractor_worker")


POLL_INTERVAL_SECONDS: int = 1800
"""30 minutes — see module docstring for the trade-off."""


_ENABLED_KV: str = "entity_extraction_enabled"
"""kv_settings flag (``0``/``1``) used as a runtime kill-switch."""


_SENTINEL: object = object()
"""Single hashable token reused as the ``BackfillRunner`` key."""


async def _is_enabled() -> bool:
    """Return ``True`` unless ``entity_extraction_enabled`` is literally ``0``.

    Default-on: the kv row is created lazily, so absence == enabled.
    """
    async with get_connection() as conn:
        value = await get_kv(conn, _ENABLED_KV)
    if value is None:
        return True
    return value.strip() != "0"


async def _list_missing() -> list[Any]:
    """Return one sentinel item iff the worker is enabled."""
    enabled = await _is_enabled()
    return [_SENTINEL] if enabled else []


async def _build_one(_key: Any) -> dict[str, int] | None:
    """Run one extraction pass and return the counter dict."""
    stats = await ingest_mentions_from_hourly_cards()
    log.info(
        "entity_extractor_worker.tick",
        cards=stats["cards"],
        entities=stats["entities"],
        mentions=stats["mentions"],
    )
    return stats


async def run_entity_extractor_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="entity-extractor-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_entity_extractor_worker",
]
