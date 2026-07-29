"""Autowake enqueue and durable outbox-dispatch use cases."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from app.application.autowake.ports import (
    AutowakeRepository,
    EnqueueResult,
    GroupTelegramDelivery,
    OwnerTelegramDelivery,
    OwnerTelegramGateway,
)
from app.domains.autowake import (
    AutowakePolicy,
    DeliveryDecision,
    DeliveryTarget,
    DeliveryTargetKind,
    ProactiveContent,
    SourceScope,
    content_rejection_reason,
)

if TYPE_CHECKING:
    from datetime import datetime

_LEASE_OWNER_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")


@dataclass(frozen=True, slots=True)
class EnqueueAutowake:
    owner_user_id: int
    is_owner: bool
    kind: str
    source: str
    source_scope: SourceScope
    text: str
    idempotency_key: str
    target: DeliveryTarget = field(default_factory=DeliveryTarget)
    group_opt_in_verified: bool = False


class AutowakeService:
    """Validates provenance and atomically records the complete delivery intent."""

    def __init__(
        self,
        repository: AutowakeRepository,
        *,
        expected_owner_user_id: int,
        policy: AutowakePolicy | None = None,
    ) -> None:
        if expected_owner_user_id <= 0:
            raise ValueError("expected_owner_user_id must be positive")
        self._repository = repository
        self._owner_id = expected_owner_user_id
        self._policy = policy or AutowakePolicy()

    async def enqueue(
        self,
        command: EnqueueAutowake,
        *,
        now: datetime,
    ) -> EnqueueResult:
        if not command.is_owner or command.owner_user_id != self._owner_id:
            raise PermissionError("autowake is restricted to the configured owner")

        group_target = command.target.kind is DeliveryTargetKind.GROUP
        if group_target and not command.group_opt_in_verified:
            raise PermissionError("Telegram group delivery requires verified opt-in")
        if group_target and command.source_scope is not SourceScope.GROUP:
            raise PermissionError("Telegram group target requires group provenance")
        content = ProactiveContent(
            kind=command.kind,
            source=command.source,
            source_scope=command.source_scope,
            text=command.text,
            idempotency_key=command.idempotency_key,
        )
        rejection = content_rejection_reason(content, allow_group=group_target)
        if rejection is not None:
            decision = DeliveryDecision(kind="reject", reason=rejection)
        else:
            state = await self._repository.policy_state(
                command.owner_user_id,
                now=now,
            )
            decision = self._policy.evaluate(now=now, state=state)

        return await self._repository.enqueue(
            owner_user_id=command.owner_user_id,
            content=content,
            target=command.target,
            decision=decision,
            fingerprint=_fingerprint(content, command.target),
            max_attempts=self._policy.config.max_attempts,
        )


class AutowakeDispatcher:
    """One-at-a-time, leased, at-least-once owner Telegram delivery."""

    def __init__(
        self,
        repository: AutowakeRepository,
        gateway: OwnerTelegramGateway,
        *,
        policy: AutowakePolicy | None = None,
        lease_owner: str,
        expected_owner_user_id: int,
        lease_seconds: int = 90,
    ) -> None:
        if not _LEASE_OWNER_PATTERN.fullmatch(lease_owner):
            raise ValueError("invalid autowake lease owner")
        if not 15 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be in 15..300")
        if expected_owner_user_id <= 0:
            raise ValueError("expected_owner_user_id must be positive")
        self._repository = repository
        self._gateway = gateway
        self._policy = policy or AutowakePolicy()
        self._lease_owner = lease_owner
        self._owner_id = expected_owner_user_id
        self._lease_seconds = lease_seconds

    async def run_once(self, *, now: datetime) -> bool:
        item = await self._repository.claim_due(
            lease_owner=self._lease_owner,
            now=now,
            lease_seconds=self._lease_seconds,
        )
        if item is None:
            return False
        if item.owner_user_id != self._owner_id:
            # A corrupted/manual row cannot redirect the configured
            # owner-only transport. Leave the lease to expire/dead-letter;
            # never pass the item to the gateway.
            raise PermissionError("autowake outbox owner does not match configured owner")

        state = await self._repository.policy_state(item.owner_user_id, now=now)
        decision = self._policy.evaluate(now=now, state=state)
        if decision.kind != "allow":
            if decision.due_at is None:
                raise RuntimeError("deferred autowake decision has no due_at")
            await self._repository.defer(
                item.id,
                lease_owner=self._lease_owner,
                due_at=decision.due_at,
                reason=decision.reason,
            )
            return True

        attempt = await self._repository.start_attempt(
            item.id,
            lease_owner=self._lease_owner,
            now=now,
        )
        try:
            if item.target.kind is DeliveryTargetKind.GROUP:
                chat_id = item.target.telegram_chat_id
                if chat_id is None:
                    raise RuntimeError("group outbox target lost its chat id")
                await self._gateway.send_group(
                    GroupTelegramDelivery(
                        owner_user_id=item.owner_user_id,
                        telegram_chat_id=chat_id,
                        text=item.content.text,
                        idempotency_key=item.content.idempotency_key,
                        kind=item.content.kind,
                    )
                )
            else:
                await self._gateway.send_owner(
                    OwnerTelegramDelivery(
                        owner_user_id=item.owner_user_id,
                        text=item.content.text,
                        idempotency_key=item.content.idempotency_key,
                        kind=item.content.kind,
                    )
                )
        except asyncio.CancelledError:
            # The transport outcome is unknown. Keep the lease so another
            # dispatcher only retries after expiry (at-least-once semantics).
            raise
        except Exception as exc:
            # Exception strings may contain URLs/tokens. Persist only the
            # bounded exception class name.
            error_code = type(exc).__name__[:80]
            await self._repository.mark_failed(
                item.id,
                lease_owner=self._lease_owner,
                failed_at=now,
                retry_at=self._policy.retry_at(now=now, attempt=attempt),
                error_code=error_code,
            )
            return True

        await self._repository.mark_delivered(
            item.id,
            lease_owner=self._lease_owner,
            delivered_at=now,
        )
        return True


def _fingerprint(content: ProactiveContent, target: DeliveryTarget) -> str:
    canonical = json.dumps(
        {
            "kind": content.kind,
            "source": content.source,
            "source_scope": content.source_scope.value,
            "text": content.text,
            "target_kind": target.kind.value,
            "target_chat_id": target.telegram_chat_id,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["AutowakeDispatcher", "AutowakeService", "EnqueueAutowake"]
