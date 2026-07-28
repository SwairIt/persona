"""Pure deterministic policy for nightly memory proposals.

No model output is trusted as an instruction.  The model may propose text and
attach evidence; this module alone decides whether the storage adapter may
change curated memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MemoryKind = Literal["fact", "preference", "person", "project", "reminder", "other"]
ProposalAction = Literal["add", "update", "noop", "delete"]
DecisionAction = Literal["add", "update", "noop", "reject"]

_ALLOWED_KINDS = frozenset({"fact", "preference", "person", "project", "reminder", "other"})


@dataclass(frozen=True, slots=True)
class DreamEvidence:
    source_kind: str
    source_ref: str
    content_hash: str
    owner_attributed: bool
    source_message_id: int | None = None
    excerpt: str | None = None
    observed_at: str | None = None

    @property
    def trusted_owner_evidence(self) -> bool:
        # A Telegram group participant is never evidence about the owner.  OCR
        # and ambient audio are also not speaker-attributed, so they may raise
        # relevance but cannot independently create an owner fact.
        return self.source_kind == "owner_chat" and self.owner_attributed


@dataclass(frozen=True, slots=True)
class DreamCandidate:
    key: str
    text: str
    kind: str
    proposed_action: ProposalAction
    score: float
    observed_count: int
    source_count: int
    evidence: tuple[DreamEvidence, ...]
    target_memory_id: int | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    id: int
    text: str
    kind: str
    pinned: bool
    active: bool = True


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    action: DecisionAction
    reason: str
    target_memory_id: int | None = None


@dataclass(frozen=True, slots=True)
class DreamPolicy:
    score_threshold: float = 0.6
    min_recall_count: int = 2
    memory_cap: int = 80

    def decide(  # noqa: PLR0911 - explicit fail-closed policy gates
        self,
        candidate: DreamCandidate,
        memories: tuple[MemorySnapshot, ...],
    ) -> PolicyDecision:
        text = " ".join(candidate.text.split())
        if len(text) < 6 or len(text) > 600:
            return PolicyDecision("reject", "text_length_out_of_bounds")
        if candidate.kind not in _ALLOWED_KINDS:
            return PolicyDecision("reject", "unsupported_memory_kind")
        if not any(item.trusted_owner_evidence for item in candidate.evidence):
            return PolicyDecision("reject", "missing_trusted_owner_evidence")
        if candidate.score <= self.score_threshold:
            return PolicyDecision("reject", "score_below_threshold")
        if candidate.source_count < self.min_recall_count:
            return PolicyDecision("reject", "insufficient_source_diversity")

        active = tuple(memory for memory in memories if memory.active)
        exact = next(
            (memory for memory in active if memory.text.casefold() == text.casefold()),
            None,
        )
        if exact is not None:
            return PolicyDecision("noop", "exact_active_memory_exists", exact.id)

        if candidate.proposed_action in {"delete", "noop"}:
            return PolicyDecision("reject", "generative_delete_or_noop_not_applicable")
        if candidate.proposed_action == "update":
            target = next(
                (memory for memory in active if memory.id == candidate.target_memory_id),
                None,
            )
            if target is None:
                return PolicyDecision("reject", "update_target_not_active")
            if target.pinned:
                return PolicyDecision("reject", "pinned_memory_is_immutable")
            return PolicyDecision("update", "trusted_supported_update", target.id)

        if candidate.target_memory_id is not None:
            return PolicyDecision("reject", "add_must_not_have_target")
        if len(active) >= self.memory_cap:
            return PolicyDecision("reject", "automatic_memory_cap_reached")
        return PolicyDecision("add", "trusted_supported_add")


__all__ = [
    "DreamCandidate",
    "DreamEvidence",
    "DreamPolicy",
    "MemorySnapshot",
    "PolicyDecision",
]
