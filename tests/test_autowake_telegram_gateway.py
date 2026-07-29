"""Fail-closed Telegram destination tests for autowake."""

from __future__ import annotations

import pytest

from app.adapters.autowake import TelegramOwnerGateway
from app.application.autowake import GroupTelegramDelivery, OwnerTelegramDelivery
from app.integrations.telegram.repository import TelegramBinding


class _API:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _Repository:
    def __init__(
        self,
        binding: TelegramBinding | None,
        allowed: set[int] | None = None,
    ) -> None:
        self.binding = binding
        self.allowed = allowed or set()

    async def get_binding(self) -> TelegramBinding | None:
        return self.binding

    async def bind_owner(
        self,
        telegram_user_id: int,
        persona_user_id: int,
    ) -> TelegramBinding:
        self.binding = TelegramBinding(telegram_user_id, persona_user_id)
        return self.binding

    async def allowed_chat_ids(self) -> set[int]:
        return set(self.allowed)


def _delivery(owner_id: int = 7) -> OwnerTelegramDelivery:
    return OwnerTelegramDelivery(
        owner_user_id=owner_id,
        text="ready",
        idempotency_key="system:ready:1",
        kind="system.ready",
    )


@pytest.mark.asyncio
async def test_gateway_sends_only_to_bound_owner_private_dm() -> None:
    api = _API()
    repository = _Repository(TelegramBinding(telegram_user_id=42, persona_user_id=7))
    gateway = TelegramOwnerGateway(  # type: ignore[arg-type]
        api,
        repository,  # type: ignore[arg-type]
        expected_owner_user_id=7,
        configured_telegram_user_id=42,
    )

    await gateway.send_owner(_delivery())

    assert api.sent == [(42, "ready")]


@pytest.mark.asyncio
async def test_gateway_rejects_wrong_owner_and_conflicting_binding() -> None:
    api = _API()
    repository = _Repository(TelegramBinding(telegram_user_id=99, persona_user_id=7))
    gateway = TelegramOwnerGateway(  # type: ignore[arg-type]
        api,
        repository,  # type: ignore[arg-type]
        expected_owner_user_id=7,
        configured_telegram_user_id=42,
    )

    with pytest.raises(PermissionError):
        await gateway.send_owner(_delivery(owner_id=8))
    with pytest.raises(PermissionError):
        await gateway.send_owner(_delivery())
    assert api.sent == []


@pytest.mark.asyncio
async def test_gateway_can_seed_the_explicit_owner_binding() -> None:
    api = _API()
    repository = _Repository(None)
    gateway = TelegramOwnerGateway(  # type: ignore[arg-type]
        api,
        repository,  # type: ignore[arg-type]
        expected_owner_user_id=7,
        configured_telegram_user_id=42,
    )

    await gateway.send_owner(_delivery())

    assert repository.binding == TelegramBinding(42, 7)
    assert api.sent == [(42, "ready")]


@pytest.mark.asyncio
async def test_group_gateway_rechecks_live_allowlist_before_every_send() -> None:
    api = _API()
    repository = _Repository(None, {-1001})
    gateway = TelegramOwnerGateway(  # type: ignore[arg-type]
        api,
        repository,  # type: ignore[arg-type]
        expected_owner_user_id=7,
    )
    delivery = GroupTelegramDelivery(
        owner_user_id=7,
        telegram_chat_id=-1001,
        text="group ready",
        idempotency_key="group:ready:1",
        kind="persona.impulse",
    )
    await gateway.send_group(delivery)
    repository.allowed.clear()
    with pytest.raises(PermissionError, match="no longer allowlisted"):
        await gateway.send_group(delivery)
    assert api.sent == [(-1001, "group ready")]
