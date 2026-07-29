"""Transport- and storage-neutral DTOs for conversation use cases."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.domains.chat import (
        ActorContext,
        ConversationId,
        ConversationSurface,
        TurnState,
    )

_TOOL_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def is_valid_tool_wire_name(name: str) -> bool:
    """Return whether one tool name is representable by the current wire parser."""
    if not _TOOL_NAME_PATTERN.fullmatch(name):
        return False
    if not name.startswith("mcp__"):
        return True
    rest = name.removeprefix("mcp__")
    if rest.count("__") != 1:
        return False
    server, tool = rest.split("__", 1)
    return bool(
        _TOOL_NAME_PATTERN.fullmatch(server)
        and _TOOL_NAME_PATTERN.fullmatch(tool)
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
class ToolTurnPolicy:
    """Bounded owner-only policy for one model/tool turn."""

    max_rounds: int = 6
    max_calls: int = 12
    max_result_chars: int = 4_000
    max_total_result_chars: int = 24_000
    allowed_tool_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= 8:
            raise ValueError("tool max_rounds must be in 1..8")
        if not 1 <= self.max_calls <= 16:
            raise ValueError("tool max_calls must be in 1..16")
        if not 256 <= self.max_result_chars <= 8_000:
            raise ValueError("tool max_result_chars must be in 256..8000")
        if not self.max_result_chars <= self.max_total_result_chars <= 32_000:
            raise ValueError("tool total result budget is invalid")
        invalid = [
            name
            for name in self.allowed_tool_names
            if not is_valid_tool_wire_name(name)
        ]
        if invalid:
            raise ValueError("tool allowlist contains an invalid name")


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    raw: str = ""

    def __post_init__(self) -> None:
        if not is_valid_tool_wire_name(self.name):
            raise ValueError("invalid tool name")
        try:
            canonical = json.dumps(
                self.arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("tool arguments must be JSON-compatible") from exc
        if len(canonical) > 16_000:
            raise ValueError("tool arguments exceed 16000 characters")
        object.__setattr__(self, "arguments", dict(self.arguments))

    @property
    def dedupe_key(self) -> str:
        encoded = json.dumps(
            self.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{self.name}:{encoded}"

    @property
    def readonly_arguments(self) -> MappingProxyType[str, Any]:
        return MappingProxyType(self.arguments)


@dataclass(frozen=True, slots=True)
class ToolExecution:
    call: ToolCall
    output: str
    is_error: bool = False


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
    tool_policy: ToolTurnPolicy | None = None

    def __post_init__(self) -> None:
        if int(self.conversation_id) <= 0:
            raise ValueError("conversation_id must be positive")
        if self.include_private_context and not self.actor.is_owner:
            raise ValueError("private context requires an owner actor")
        if self.allow_tools and not self.actor.is_owner:
            raise ValueError("tools require an owner actor")
        if self.tool_policy is not None and not self.allow_tools:
            raise ValueError("tool_policy requires allow_tools")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

    @property
    def model_text(self) -> str:
        clean = self.text.strip()
        label = (self.source_label or "").strip()
        return f"[{label}] {clean}" if label else clean

    @property
    def effective_tool_policy(self) -> ToolTurnPolicy | None:
        if not self.allow_tools:
            return None
        return self.tool_policy or ToolTurnPolicy()


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
