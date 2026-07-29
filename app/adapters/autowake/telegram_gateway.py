"""Telegram transport for privacy-scoped proactive Persona messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.autowake import GroupTelegramDelivery, OwnerTelegramDelivery
    from app.integrations.telegram.api import TelegramBotAPI
    from app.integrations.telegram.repository import TelegramRepository


class TelegramOwnerGateway:
    """Resolve the owner DM and re-authorize explicit group targets at send time."""

    def __init__(
        self,
        api: TelegramBotAPI,
        repository: TelegramRepository,
        *,
        expected_owner_user_id: int,
        configured_telegram_user_id: int | None = None,
        configured_allowed_chat_ids: frozenset[int] = frozenset(),
    ) -> None:
        if expected_owner_user_id <= 0:
            raise ValueError("expected_owner_user_id must be positive")
        if configured_telegram_user_id is not None and configured_telegram_user_id <= 0:
            raise ValueError("configured_telegram_user_id must be positive")
        self._api = api
        self._repository = repository
        self._owner_id = expected_owner_user_id
        self._configured_telegram_id = configured_telegram_user_id
        self._configured_allowed_chat_ids = frozenset(configured_allowed_chat_ids)

    async def send_owner(self, delivery: OwnerTelegramDelivery) -> None:
        if delivery.owner_user_id != self._owner_id:
            raise PermissionError("autowake delivery does not belong to the configured owner")

        binding = await self._repository.get_binding()
        if binding is None and self._configured_telegram_id is not None:
            binding = await self._repository.bind_owner(
                self._configured_telegram_id,
                self._owner_id,
            )
        if binding is None:
            raise RuntimeError("Telegram owner is not paired")
        if binding.persona_user_id != self._owner_id:
            raise PermissionError("Telegram binding belongs to a different Persona owner")
        if (
            self._configured_telegram_id is not None
            and binding.telegram_user_id != self._configured_telegram_id
        ):
            raise PermissionError("Telegram binding conflicts with the configured owner")
        if binding.telegram_user_id <= 0:
            raise PermissionError("Telegram owner binding is not a private user chat")

        # A Telegram private chat uses the owner's positive user id as chat id.
        # No producer or outbox row can redirect this destination.
        await self._api.send_message(binding.telegram_user_id, delivery.text)

    async def send_group(self, delivery: GroupTelegramDelivery) -> None:
        if delivery.owner_user_id != self._owner_id:
            raise PermissionError("autowake delivery does not belong to the configured owner")
        if delivery.telegram_chat_id >= 0:
            raise PermissionError("autowake group target must be a Telegram group")
        allowed = (
            await self._repository.allowed_chat_ids()
        ) | set(self._configured_allowed_chat_ids)
        if delivery.telegram_chat_id not in allowed:
            raise PermissionError("Telegram group is no longer allowlisted")
        await self._api.send_message(delivery.telegram_chat_id, delivery.text)


__all__ = ["TelegramOwnerGateway"]
