"""Ports used by the ambient group application service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.application.ambient_group.dto import AmbientGroupTurn


class AmbientGroupDecisionPort(Protocol):
    async def should_reply(self, turn: AmbientGroupTurn) -> bool: ...


class AmbientGroupTurnPort(Protocol):
    async def persist(self, turn: AmbientGroupTurn) -> None: ...

    async def reply(self, turn: AmbientGroupTurn) -> str:
        """Persist the user turn exactly once, then generate an isolated reply."""
        ...


class MonotonicClock(Protocol):
    def now(self) -> float: ...
