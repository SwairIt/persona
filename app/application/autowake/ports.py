"""Infrastructure-neutral ports for the autowake use case."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from app.domains.autowake import DeliveryDecision, DeliveryState, ProactiveContent


class AutowakeStateError(RuntimeError):
    """A durable outbox transition violated its lease/state contract."""


class IdempotencyConflict(AutowakeStateError):
    """An idempotency key was reused for different content."""


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    event_id: int
    outbox_id: int | None
    created: bool
    accepted: bool
    reason: str
    due_at: datetime | None


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: int
    event_id: int
    session_id: int
    message_id: int
    owner_user_id: int
    content: ProactiveContent
    due_at: datetime
    attempts: int
    max_attempts: int
    lease_owner: str


@dataclass(frozen=True, slots=True)
class OwnerTelegramDelivery:
    """No chat id by design: the adapter can only resolve the owner's DM."""

    owner_user_id: int
    text: str
    idempotency_key: str
    kind: str


class AutowakeRepository(Protocol):
    async def policy_state(
        self,
        owner_user_id: int,
        *,
        now: datetime,
    ) -> DeliveryState: ...

    async def enqueue(
        self,
        *,
        owner_user_id: int,
        content: ProactiveContent,
        decision: DeliveryDecision,
        fingerprint: str,
        max_attempts: int,
    ) -> EnqueueResult: ...

    async def claim_due(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> OutboxItem | None: ...

    async def start_attempt(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        now: datetime,
    ) -> int: ...

    async def defer(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        due_at: datetime,
        reason: str,
    ) -> None: ...

    async def mark_delivered(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        delivered_at: datetime,
    ) -> None: ...

    async def mark_failed(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        failed_at: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> str: ...


class OwnerTelegramGateway(Protocol):
    async def send_owner(self, delivery: OwnerTelegramDelivery) -> None: ...


__all__ = [
    "AutowakeRepository",
    "AutowakeStateError",
    "EnqueueResult",
    "IdempotencyConflict",
    "OutboxItem",
    "OwnerTelegramDelivery",
    "OwnerTelegramGateway",
]
