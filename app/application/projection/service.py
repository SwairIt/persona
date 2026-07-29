"""Dispatch one leased projection without holding an infrastructure lock."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.projection.ports import ProjectionCapabilityUnavailable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from app.application.projection.ports import ProjectionGateway, ProjectionOutboxPort
    from app.domains.projection import ProjectionKind


class ProjectionDispatcher:
    def __init__(
        self,
        outbox: ProjectionOutboxPort,
        gateways: Mapping[ProjectionKind, ProjectionGateway],
        *,
        expected_owner_user_id: int,
        lease_owner: str,
        lease_seconds: int = 180,
    ) -> None:
        self._outbox = outbox
        self._gateways = dict(gateways)
        self._owner_user_id = expected_owner_user_id
        self._lease_owner = lease_owner
        self._lease_seconds = max(30, min(int(lease_seconds), 900))

    async def run_once(self, *, now: datetime) -> bool:
        job = await self._outbox.claim(
            expected_owner_user_id=self._owner_user_id,
            lease_owner=self._lease_owner,
            now=now,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        gateway = self._gateways.get(job.kind)
        if gateway is None:
            await self._outbox.fail(
                job,
                error_code="projector_not_registered",
                capability_status="unavailable",
                now=now,
            )
            return True
        try:
            # Network/model work happens strictly between repository calls.
            payload = await gateway.project(job)
        except ProjectionCapabilityUnavailable as exc:
            await self._outbox.fail(
                job,
                error_code=exc.code,
                capability_status="unavailable" if exc.unavailable else "degraded",
                now=now,
            )
            return True
        except Exception:
            await self._outbox.fail(
                job,
                error_code="projector_error",
                capability_status="degraded",
                now=now,
            )
            return True
        await self._outbox.complete(job, payload, now=now)
        return True


__all__ = ["ProjectionDispatcher"]
