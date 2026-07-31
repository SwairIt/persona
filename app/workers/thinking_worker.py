"""The thinking loop worker — idle-gated, budget-capped, owner-first.

Drives Persona's self-directed thought chains (``app.thinking.loop``) the
same way ``app.workers.dream_worker`` drives the nightly dream cycle: a
single decision (:func:`tick`) wrapped in a polling loop by
:func:`run_thinking_worker`.

Owner priority is an invariant, not an optimisation: the loop must never
think while the owner might be waiting on the model. ``tick`` therefore
checks, in this exact order, ``enabled`` → owner idleness → the daily
budget → an already-open chain → seeding a new one. Every check that finds
a reason not to think returns immediately without touching the model.

A chain can get stuck if the model keeps failing right at its cap: each
call to ``advance_chain`` retries the same conclusion request, and a
persistently broken model would burn the whole daily budget retrying a
chain that can never close. ``_CONSECUTIVE_FAILURES`` tracks consecutive
``"failed"`` outcomes per chain (in-process; a restart resets the count,
which is fine — it is a circuit breaker, not an audit trail) and, after
:data:`_MAX_CONSECUTIVE_FAILURES` in a row, force-closes the chain with a
plain fallback conclusion so the loop moves on. The chain is never
silently deleted — the owner can still see in the diary that thinking was
interrupted.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.auth.owner import get_owner_user_id
from app.chat.reflection import _is_quiet
from app.domains.autowake.policy import SourceScope
from app.logging_setup import get_logger
from app.thinking.loop import advance_chain, next_seed_kind, seed_chain
from app.thinking.settings import load_thinking_settings
from app.thinking.store import ThoughtStore
from app.workers.heartbeat import beat

_SOURCE_SCOPE_BY_VALUE = {scope.value: scope for scope in SourceScope}

if TYPE_CHECKING:
    from datetime import datetime

    from app.thinking.settings import ThinkingSettings

log = get_logger("persona.workers.thinking")

_MAX_CONSECUTIVE_FAILURES: int = 3
_FALLBACK_CONCLUSION: str = (
    "Мысль прервалась: модель не смогла подвести итог несколько раз подряд, "
    "цепочка закрыта без вывода."
)

_PRODUCTIVE_SLEEP_SECONDS: float = 60.0
_IDLE_SLEEP_SECONDS: float = 300.0

# In-process circuit breaker: consecutive "failed" advance_chain outcomes per
# chain_id. Not persisted — a worker restart resetting this is acceptable,
# since it only guards against burning the daily budget on a single stuck
# chain within one process lifetime.
_CONSECUTIVE_FAILURES: dict[int, int] = {}


async def _deliver_research_conclusion(
    store: ThoughtStore, chain_id: int, *, now: datetime
) -> None:
    """After a ``research`` chain concludes, send the answer back into the
    chat that asked -- never into the owner's private diary/DM as
    owner-private data when the source was a group. Best-effort: a delivery
    failure must never fail the thinking tick itself, since the conclusion
    is already safely recorded in the diary regardless."""
    try:
        chain = await store.get_chain(chain_id)
        if chain is None or chain.get("seed_kind") != "research":
            return
        steps = await store.chain_steps(chain_id)
        if not steps or steps[-1].get("kind") != "conclusion":
            return
        scope = _SOURCE_SCOPE_BY_VALUE.get(str(chain.get("source_scope")))
        if scope is None:
            return
        chat_id = chain.get("source_chat_id")

        from app.adapters.autowake import SqliteAutowakeRepository  # noqa: PLC0415
        from app.application.autowake import (  # noqa: PLC0415
            AutowakeService,
            enqueue_completed_research,
        )

        owner_id = int(chain["persona_user_id"])
        service = AutowakeService(SqliteAutowakeRepository(), expected_owner_user_id=owner_id)
        await enqueue_completed_research(
            service,
            owner_user_id=owner_id,
            chain_id=chain_id,
            topic=str(steps[0].get("text") or ""),
            conclusion=str(steps[-1].get("text") or ""),
            completed_at=now,
            source_scope=scope,
            chat_id=int(chat_id) if chat_id is not None else None,
        )
    except Exception:  # noqa: BLE001 — delivery is best-effort, never breaks the tick
        log.warning("thinking.research.delivery_failed", chain_id=chain_id)


async def tick(
    store: ThoughtStore,
    settings: ThinkingSettings,
    *,
    persona_user_id: int,
    now: datetime,
    client: Any | None = None,
) -> str:
    """Make one thinking-loop decision. Never raises for expected states.

    Returns one of ``"disabled"``, ``"busy"``, ``"budget"``, ``"stepped"``,
    ``"closed"``, ``"failed"``, ``"seeded"`` or ``"idle"``. The order of
    checks below is the design: owner priority first, budget second, and
    only then does any chain work happen.
    """
    if not settings.enabled:
        return "disabled"

    if not await _is_quiet(now):
        return "busy"

    used = await store.steps_used_today(persona_user_id)
    if used >= settings.daily_budget:
        return "budget"

    open_chain = await store.oldest_open_chain(persona_user_id)
    if open_chain is not None:
        chain_id = int(open_chain["chain_id"])
        outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
        if outcome != "failed":
            _CONSECUTIVE_FAILURES.pop(chain_id, None)
            if outcome == "closed":
                await _deliver_research_conclusion(store, chain_id, now=now)
            return outcome

        failures = _CONSECUTIVE_FAILURES.get(chain_id, 0) + 1
        _CONSECUTIVE_FAILURES[chain_id] = failures
        if failures >= _MAX_CONSECUTIVE_FAILURES:
            await store.close_chain(
                chain_id, conclusion=_FALLBACK_CONCLUSION, certainty="guess"
            )
            _CONSECUTIVE_FAILURES.pop(chain_id, None)
            log.warning(
                "thinking.chain.force_closed",
                chain_id=chain_id,
                consecutive_failures=failures,
            )
            await _deliver_research_conclusion(store, chain_id, now=now)
            return "closed"
        return "failed"

    seed_kind = next_seed_kind(settings, None)
    chain_id = await seed_chain(
        store,
        persona_user_id=persona_user_id,
        seed_kind=seed_kind,
        source_scope=SourceScope.OWNER_PRIVATE,
        source_session_id=None,
        client=client,
        model=settings.model,
    )
    if chain_id is None:
        return "idle"
    return "seeded"


async def _job_tick() -> str:
    """One iteration: fresh settings, resolved owner, real store/clock."""
    from datetime import UTC, datetime  # noqa: PLC0415

    owner = await get_owner_user_id()
    if owner is None:
        return "idle"
    settings = await load_thinking_settings()
    store = ThoughtStore()
    return await tick(store, settings, persona_user_id=owner, now=datetime.now(UTC))


async def run_thinking_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point: poll ``tick`` forever, idle-friendly cadence.

    Settings are reloaded every iteration so toggling the feature on the
    site takes effect without a restart. A productive tick (``"stepped"``,
    ``"closed"`` or ``"seeded"``) is followed by a short sleep so a chain
    can develop promptly while the owner stays idle; anything else
    (``"disabled"``, ``"busy"``, ``"budget"``, ``"failed"``, ``"idle"``)
    backs off to a longer sleep. Each iteration is wrapped in try/except so
    one failure never kills the worker.
    """
    while stop_event is None or not stop_event.is_set():
        try:
            outcome = await _job_tick()
        except Exception:  # noqa: BLE001 — a worker must never die from this
            log.exception("thinking.tick.failed")
            outcome = "idle"
        await beat("thinking", status=outcome)
        sleep_seconds = (
            _PRODUCTIVE_SLEEP_SECONDS
            if outcome in {"stepped", "closed", "seeded"}
            else _IDLE_SLEEP_SECONDS
        )
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_seconds)
            else:
                await asyncio.sleep(sleep_seconds)
        except TimeoutError:
            pass


__all__ = ["run_thinking_worker", "tick"]
