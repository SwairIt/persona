"""Transport- and persistence-neutral projection values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProjectionKind(StrEnum):
    GRAPH = "graph"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class ProjectionEvidence:
    id: int
    source_kind: str
    owner_attributed: bool
    content_hash: str
    excerpt: str | None = None

    @property
    def trusted_owner_chat(self) -> bool:
        return self.source_kind == "owner_chat" and self.owner_attributed


@dataclass(frozen=True, slots=True)
class ProjectionSource:
    owner_user_id: int
    dream_revision_id: int
    memory_id: int
    text: str
    content_hash: str
    revision_action: str
    candidate_status: str
    memory_pinned: bool
    memory_active: bool
    evidence: tuple[ProjectionEvidence, ...]


@dataclass(frozen=True, slots=True)
class ProjectionJob:
    id: int
    kind: ProjectionKind
    source: ProjectionSource
    attempts: int
    max_attempts: int
    lease_owner: str


@dataclass(frozen=True, slots=True)
class GraphTriple:
    subject: str
    relation: str
    object: str


@dataclass(frozen=True, slots=True)
class GraphProjection:
    triples: tuple[GraphTriple, ...]

    @property
    def units(self) -> int:
        return len(self.triples)


@dataclass(frozen=True, slots=True)
class EmbeddingProjection:
    vector: tuple[float, ...]
    model_name: str

    @property
    def units(self) -> int:
        return len(self.vector)


ProjectionPayload = GraphProjection | EmbeddingProjection

__all__ = [
    "EmbeddingProjection",
    "GraphProjection",
    "GraphTriple",
    "ProjectionEvidence",
    "ProjectionJob",
    "ProjectionKind",
    "ProjectionPayload",
    "ProjectionSource",
]
