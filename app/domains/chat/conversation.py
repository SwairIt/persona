"""Framework-free conversation identity, states and domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

TenantId = NewType("TenantId", int)
UserId = NewType("UserId", int)
ConversationId = NewType("ConversationId", int)


class ConversationSurface(StrEnum):
    """Ingress surface for policy decisions, never a transport object."""

    WEB = "web"
    TELEGRAM = "telegram"
    AUTOMATION = "automation"


class TurnState(StrEnum):
    """Observable states of one conversation turn."""

    ACCEPTED = "accepted"
    CONTEXT_READY = "context_ready"
    GENERATING = "generating"
    TOOL_RUNNING = "tool_running"
    TOOL_COMPLETED = "tool_completed"
    PERSISTING = "persisting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ActorContext:
    """The authenticated principal and tenant scope for one use case."""

    tenant_id: TenantId
    user_id: UserId
    is_owner: bool

    def __post_init__(self) -> None:
        if int(self.tenant_id) <= 0 or int(self.user_id) <= 0:
            raise ValueError("actor and tenant identifiers must be positive")
        if int(self.tenant_id) != int(self.user_id):
            raise ValueError("cross-tenant conversation access is forbidden")


class ConversationError(RuntimeError):
    """Base class for errors safe to map at an entrypoint."""


class InvalidTurn(ConversationError):
    """The command contains no usable user input."""


class ConversationNotFound(ConversationError):
    """The requested conversation does not exist in the actor's tenant."""


class ConversationAccessDenied(ConversationError):
    """The actor cannot access the requested conversation."""


class ModelUnavailable(ConversationError):
    """No configured model can serve the turn."""


class TurnGenerationFailed(ConversationError):
    """The provider failed after the turn had been accepted."""
