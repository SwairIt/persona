"""Transport- and provider-neutral ambient group DTOs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AmbientGroupTurn:
    tenant_id: int
    conversation_id: int
    external_chat_id: int
    update_id: int
    message_id: int
    text: str
    sender_label: str
    chat_title: str

    def __post_init__(self) -> None:
        if self.tenant_id <= 0 or self.conversation_id <= 0:
            raise ValueError("ambient group turn requires a valid tenant and conversation")
        if not self.text.strip():
            raise ValueError("ambient group turn text is required")


@dataclass(frozen=True, slots=True)
class AmbientGroupOutcome:
    reply: str = ""

    @property
    def should_send(self) -> bool:
        return bool(self.reply.strip())
