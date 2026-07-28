"""Transport- and storage-neutral DTOs for conversation use cases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.domains.chat import (
        ActorContext,
        ConversationId,
        ConversationSurface,
        TurnState,
    )


@dataclass(frozen=True, slots=True)
class ConversationMessage:
    id: int
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class ResolvedConversation:
    id: ConversationId
    tenant_id: int
    title: str
    provider: str | None = None
    model: str | None = None
    summary: str | None = None


@dataclass(frozen=True, slots=True)
class TurnCommand:
    actor: ActorContext
    surface: ConversationSurface
    conversation_id: ConversationId
    text: str
    image_data_url: str | None = None
    source_label: str | None = None
    include_private_context: bool = True
    allow_tools: bool = False
    max_tokens: int = 4096
    temperature: float = 0.7
    correlation_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if int(self.conversation_id) <= 0:
            raise ValueError("conversation_id must be positive")
        if self.include_private_context and not self.actor.is_owner:
            raise ValueError("private context requires an owner actor")
        if self.allow_tools and not self.actor.is_owner:
            raise ValueError("tools require an owner actor")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

    @property
    def model_text(self) -> str:
        clean = self.text.strip()
        label = (self.source_label or "").strip()
        return f"[{label}] {clean}" if label else clean


@dataclass(frozen=True, slots=True)
class PreparedContext:
    system: str
    user: str
    history: tuple[ConversationMessage, ...]


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system: str
    user: str
    max_tokens: int
    temperature: float
    image_data_url: str | None = None
    preferred_model: str | None = None
    purpose: str = "conversation"


@dataclass(frozen=True, slots=True)
class ModelUsage:
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TurnResult:
    conversation_id: ConversationId
    user_message_id: int
    assistant_message_id: int
    answer: str
    elapsed_ms: int
    usage: ModelUsage


@dataclass(frozen=True, slots=True)
class TurnEvent:
    state: TurnState
    text: str = ""
    detail: str = ""
    result: TurnResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
