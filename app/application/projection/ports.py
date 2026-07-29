"""Application boundaries for the projection outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime

    from app.domains.projection import ProjectionJob, ProjectionKind
    from app.domains.projection.model import ProjectionPayload


class ProjectionCapabilityUnavailable(RuntimeError):
    """An optional projector cannot currently execute."""

    def __init__(self, code: str, *, unavailable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.unavailable = unavailable


class ProjectionGateway(Protocol):
    kind: ProjectionKind

    async def project(self, job: ProjectionJob) -> ProjectionPayload: ...


class ProjectionOutboxPort(Protocol):
    async def claim(
        self,
        *,
        expected_owner_user_id: int,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> ProjectionJob | None: ...

    async def complete(
        self,
        job: ProjectionJob,
        payload: ProjectionPayload,
        *,
        now: datetime,
    ) -> str: ...

    async def fail(
        self,
        job: ProjectionJob,
        *,
        error_code: str,
        capability_status: str,
        now: datetime,
    ) -> str: ...


__all__ = [
    "ProjectionCapabilityUnavailable",
    "ProjectionGateway",
    "ProjectionOutboxPort",
]
