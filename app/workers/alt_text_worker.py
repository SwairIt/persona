"""Background backfill of per-shot LLM alt-text (v1.32).

Wakes every :data:`POLL_INTERVAL_SECONDS` (10 min). On each tick:

1. Reads the gate kv row ``shot_alt_text_enabled`` — when the value is
   anything other than ``"1"`` the runner sees an empty missing-list and
   the :class:`BackfillRunner` sleeps until the next poll.
2. Selects up to :data:`_BATCH_LIMIT` recent ``screenshots`` rows with
   no ``alt_text`` and a non-empty ``ocr_text``, freshest first.
3. Calls :func:`app.llm.shot_alt_text.generate_alt_text` on each.

Per-row failures bubble up as status strings inside
:class:`~app.llm.shot_alt_text.AltTextResult` and are logged at INFO by
:class:`BackfillRunner` — they never crash the loop. Only
``asyncio.CancelledError`` exits the worker.

The lifespan-task wiring lives in the coordinator module — this file
deliberately exposes only :func:`run_alt_text_worker` and never modifies
the lifespan tasks list itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.llm.shot_alt_text import generate_alt_text
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.alt_text")

POLL_INTERVAL_SECONDS: int = 600
"""10 minutes. Alt-text is a paid LLM call against the user's BYO key,
so polling more aggressively than this just burns rate-limit budget
without a UX win — a fresh screenshot is no less useful with a one-line
description that arrived ten minutes later."""

_KV_ENABLED: str = "shot_alt_text_enabled"
"""kv_settings row that gates the worker. Default OFF — the user opts
in via the settings page so we never silently bill against their BYO
key on the very first boot after an upgrade."""

_BATCH_LIMIT: int = 5
"""Per-tick row cap. Five is enough to make steady progress on a
backlog (30 shots / hour) while keeping the worst-case tick under a
handful of API calls — the user might be on a small free tier."""


async def _gate_enabled() -> bool:
    """Return ``True`` when the kv row is set to ``"1"``.

    A missing row, an empty value, or anything else returns ``False`` so
    a fresh install stays opted-out by default. Read failures are
    swallowed and treated as opted-out — a transient DB error must never
    accidentally enable a paid feature.
    """
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, _KV_ENABLED)
    except Exception as exc:
        log.warning("alt_text.gate.read_failed", error=str(exc))
        return False
    if raw is None:
        return False
    return raw.strip() == "1"


async def _list_missing() -> list[int]:
    """Return up to :data:`_BATCH_LIMIT` shot ids awaiting an alt-text.

    Returns an empty list when the gate is disabled — :class:`BackfillRunner`
    then sleeps for :data:`POLL_INTERVAL_SECONDS` without doing any work.

    Ordered ``captured_at DESC`` so the user sees the freshest shots
    described first — historical backfill catches up over subsequent
    ticks.
    """
    if not await _gate_enabled():
        return []

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id FROM screenshots "
                "WHERE alt_text IS NULL "
                "  AND ocr_text IS NOT NULL "
                "  AND TRIM(ocr_text) <> '' "
                "ORDER BY captured_at DESC "
                "LIMIT ?",
                (_BATCH_LIMIT,),
            )
            rows = await cursor.fetchall()
    except Exception as exc:
        log.exception("alt_text.select_failed", error=str(exc))
        return []
    return [int(row["id"]) for row in rows]


async def _build_one(shot_id: int) -> dict[str, Any] | None:
    """Generate + cache the alt-text for ``shot_id``.

    Returns the result dict on a successful write so :class:`BackfillRunner`
    counts it toward the "built" tally; returns ``None`` for cache hits
    and skipped rows so the cycle log stays accurate.
    """
    result = await generate_alt_text(shot_id)
    if result["status"] == "ok":
        return dict(result)
    log.info(
        "alt_text.row.skipped",
        shot_id=shot_id,
        status=result["status"],
    )
    return None


async def run_alt_text_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="alt-text-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = ["POLL_INTERVAL_SECONDS", "run_alt_text_worker"]
