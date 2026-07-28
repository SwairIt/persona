"""Use-case orchestration for proposal-only nightly memory changes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.memory.ports import DreamApplySummary

if TYPE_CHECKING:
    from app.application.memory.ports import DreamLedgerPort, DreamRunLease
    from app.domains.memory.dream import DreamCandidate, DreamPolicy


class DreamLedgerService:
    def __init__(self, ledger: DreamLedgerPort) -> None:
        self._ledger = ledger

    async def apply_proposals(
        self,
        lease: DreamRunLease,
        proposals: tuple[DreamCandidate, ...],
        policy: DreamPolicy,
    ) -> DreamApplySummary:
        """Persist all proposals before applying any of them."""
        stored = await self._ledger.store_proposals(lease, proposals)
        applied = 0
        rejected = 0
        noops = 0
        for candidate in stored:
            memories = await self._ledger.list_memories(lease.user_id)
            decision = policy.decide(candidate, memories)
            result = await self._ledger.apply_decision(lease, candidate, decision)
            if result == "applied":
                applied += 1
            elif result == "noop":
                noops += 1
            else:
                rejected += 1
        return DreamApplySummary(
            candidates=len(stored),
            applied=applied,
            rejected=rejected,
            noops=noops,
        )


__all__ = ["DreamLedgerService"]
