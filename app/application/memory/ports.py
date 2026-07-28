"""Storage boundary for the dream proposal workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.domains.memory.dream import DreamCandidate, MemorySnapshot, PolicyDecision


@dataclass(frozen=True, slots=True)
class DreamRunLease:
    run_id: int
    user_id: int
    worker_id: str
    acquired: bool
    status: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class DreamApplySummary:
    candidates: int
    applied: int
    rejected: int
    noops: int


@dataclass(frozen=True, slots=True)
class DreamCompletionReport:
    dream_text: str = ""
    source_message_ids: tuple[int, ...] = ()
    consolidations: int = 0
    conflicts: int = 0
    impact_score: float = 0.0


class DreamLedgerPort(Protocol):
    async def acquire_run(
        self,
        *,
        user_id: int,
        idempotency_key: str,
        worker_id: str,
        input_cursor: int,
        config: dict[str, object],
        lease_seconds: int,
    ) -> DreamRunLease: ...

    async def heartbeat(
        self, lease: DreamRunLease, *, lease_seconds: int
    ) -> None: ...

    async def store_proposals(
        self, lease: DreamRunLease, candidates: tuple[DreamCandidate, ...]
    ) -> tuple[DreamCandidate, ...]: ...

    async def list_memories(self, user_id: int) -> tuple[MemorySnapshot, ...]: ...

    async def apply_decision(
        self,
        lease: DreamRunLease,
        candidate: DreamCandidate,
        decision: PolicyDecision,
    ) -> str: ...

    async def complete_run(
        self,
        lease: DreamRunLease,
        *,
        safe_cursor: int,
        summary: DreamApplySummary,
        report: DreamCompletionReport,
    ) -> None: ...

    async def retry_run(
        self,
        lease: DreamRunLease,
        *,
        error: str,
        retry_seconds: int,
        safe_cursor: int,
    ) -> None: ...


__all__ = [
    "DreamApplySummary",
    "DreamCompletionReport",
    "DreamLedgerPort",
    "DreamRunLease",
]
