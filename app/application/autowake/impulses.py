"""Bounded proactive Persona impulse use case.

The application layer decides *whether* an LLM may be called before gathering
or generating content. Delivery itself remains the durable autowake outbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.application.autowake.ports import IdempotencyConflict
from app.application.autowake.service import AutowakeService, EnqueueAutowake
from app.domains.autowake import (
    AutowakePolicy,
    DeliveryTarget,
    DeliveryTargetKind,
    SourceScope,
)

if TYPE_CHECKING:
    from datetime import datetime

    from app.application.autowake.ports import AutowakeRepository, EnqueueResult

_MAX_EXCERPTS = 12
_MAX_EXCERPT_CHARS = 2_000
_MAX_CONTEXT_CHARS = 8_000
_MAX_MESSAGE_CHARS = 600
_SLOT_SECONDS = 30 * 60


@dataclass(frozen=True, slots=True)
class ImpulseContext:
    """Already privacy-scoped evidence supplied to the LLM."""

    owner_user_id: int
    target: DeliveryTarget
    source_scope: SourceScope
    provenance: str
    excerpts: tuple[str, ...]
    group_opt_in_verified: bool = False

    def __post_init__(self) -> None:
        if self.owner_user_id <= 0:
            raise ValueError("impulse owner must be positive")
        if not self.excerpts or len(self.excerpts) > _MAX_EXCERPTS:
            raise ValueError("impulse context must contain 1..12 excerpts")
        if any(not item.strip() or len(item) > _MAX_EXCERPT_CHARS for item in self.excerpts):
            raise ValueError("impulse excerpt is empty or too large")
        if sum(len(item) for item in self.excerpts) > _MAX_CONTEXT_CHARS:
            raise ValueError("impulse context is too large")

        group = self.target.kind is DeliveryTargetKind.GROUP
        if group:
            if (
                self.source_scope is not SourceScope.GROUP
                or self.provenance != "telegram_group"
                or not self.group_opt_in_verified
            ):
                raise ValueError("group impulse context lacks isolated opt-in provenance")
        elif (
            self.source_scope is SourceScope.GROUP
            or self.provenance != "telegram_owner_dm"
            or self.group_opt_in_verified
        ):
            raise ValueError("owner impulse context must be owner-private")


@dataclass(frozen=True, slots=True)
class ImpulseOutcome:
    emitted: bool
    reason: str
    enqueue_result: EnqueueResult | None = None


class ImpulseContextPort(Protocol):
    async def next_context(
        self,
        *,
        owner_user_id: int,
        now: datetime,
    ) -> ImpulseContext | None: ...


class ImpulseDecisionPort(Protocol):
    async def decide(self, context: ImpulseContext) -> str | None: ...


class PersonaImpulseProducer:
    """Generate at most one idempotent delivery intent per target/half-hour."""

    def __init__(
        self,
        repository: AutowakeRepository,
        autowake: AutowakeService,
        context_source: ImpulseContextPort,
        decision: ImpulseDecisionPort,
        *,
        owner_user_id: int,
        policy: AutowakePolicy | None = None,
    ) -> None:
        if owner_user_id <= 0:
            raise ValueError("owner_user_id must be positive")
        self._repository = repository
        self._autowake = autowake
        self._context_source = context_source
        self._decision = decision
        self._owner_id = owner_user_id
        self._policy = policy or AutowakePolicy()

    async def run_once(self, *, now: datetime) -> ImpulseOutcome:
        # Quiet hours, cooldown and daily cap suppress both context gathering
        # and LLM cost. Autowake evaluates again during enqueue/dispatch to
        # close races with other proactive producers.
        state = await self._repository.policy_state(self._owner_id, now=now)
        gate = self._policy.evaluate(now=now, state=state)
        if gate.kind != "allow":
            return ImpulseOutcome(emitted=False, reason=gate.reason)

        context = await self._context_source.next_context(
            owner_user_id=self._owner_id,
            now=now,
        )
        if context is None:
            return ImpulseOutcome(emitted=False, reason="no_recent_context")
        if context.owner_user_id != self._owner_id:
            raise PermissionError("impulse context belongs to another owner")

        # This is deliberately outside every DB transaction owned by the
        # repository/context adapter.
        raw = await self._decision.decide(context)
        text = str(raw or "").strip()
        if not text or text.upper() == "SILENT":
            return ImpulseOutcome(emitted=False, reason="model_silent")
        text = text[:_MAX_MESSAGE_CHARS].strip()
        lowered = text.casefold()
        if not text or "<tool" in lowered or "</tool" in lowered:
            return ImpulseOutcome(emitted=False, reason="unsafe_model_output")

        command = EnqueueAutowake(
            owner_user_id=self._owner_id,
            is_owner=True,
            kind="persona.impulse",
            source=(
                "telegram_group"
                if context.target.kind is DeliveryTargetKind.GROUP
                else "persona_impulse"
            ),
            source_scope=context.source_scope,
            text=text,
            idempotency_key=_idempotency_key(context.target, now),
            target=context.target,
            group_opt_in_verified=context.group_opt_in_verified,
        )
        try:
            result = await self._autowake.enqueue(command, now=now)
        except IdempotencyConflict:
            # The same slot was already generated with different wording.
            # Never create a second queued message merely because the LLM is
            # nondeterministic.
            return ImpulseOutcome(emitted=False, reason="duplicate_slot")
        return ImpulseOutcome(
            emitted=result.created and result.accepted,
            reason=result.reason,
            enqueue_result=result,
        )


def _idempotency_key(target: DeliveryTarget, now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("impulse datetime must be timezone-aware")
    slot = int(now.timestamp()) // _SLOT_SECONDS
    destination = (
        str(target.telegram_chat_id)
        if target.kind is DeliveryTargetKind.GROUP
        else "owner"
    )
    return f"persona-impulse:{target.kind.value}:{destination}:{slot}"


__all__ = [
    "ImpulseContext",
    "ImpulseContextPort",
    "ImpulseDecisionPort",
    "ImpulseOutcome",
    "PersonaImpulseProducer",
]
