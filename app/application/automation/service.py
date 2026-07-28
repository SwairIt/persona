"""Owner-only orchestration for outbound remote browser jobs."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.application.automation.contracts import BrowserCommand
    from app.application.automation.ports import RemoteBrowserJobPort


class BrowserExecutionError(RuntimeError):
    """Remote browser returned a terminal failure."""


class BrowserExecutionTimeout(BrowserExecutionError):
    """Remote PC did not finish within the caller's bounded wait."""


class RemoteBrowserService:
    """Serialize a chat's browser actions and await a durable job result."""

    def __init__(
        self,
        jobs: RemoteBrowserJobPort,
        *,
        execution_timeout_seconds: float = 75.0,
        poll_fallback_seconds: float = 2.0,
    ) -> None:
        self._jobs = jobs
        self._timeout = max(1.0, execution_timeout_seconds)
        self._poll = max(0.1, poll_fallback_seconds)
        self._session_locks: defaultdict[tuple[int, int], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def execute(self, command: BrowserCommand) -> dict[str, Any]:
        """Execute one validated action.

        Cancellation marks the durable job before propagating so a late worker
        cannot publish a result into a caller that has already disappeared.
        """
        key = (command.owner_user_id, command.session_id)
        async with self._session_locks[key]:
            job_id = await self._jobs.enqueue(command)
            try:
                return await self._wait(job_id)
            except asyncio.CancelledError:
                await self._jobs.cancel(job_id, "request cancelled")
                raise
            finally:
                await self._jobs.scrub_sensitive(job_id)
                self._jobs.forget(job_id)

    async def _wait(self, job_id: int) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout
        while True:
            job = await self._jobs.get(job_id)
            if job is None:
                raise BrowserExecutionError("remote browser job disappeared")
            if job.status == "done":
                return job.result or {}
            if job.status in {"error", "cancelled"}:
                raise BrowserExecutionError(job.error or f"browser job {job.status}")

            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._jobs.cancel(job_id, "server execution timeout")
                raise BrowserExecutionTimeout(
                    f"remote browser did not answer within {self._timeout:g}s"
                )
            await self._jobs.wait_for_change(job_id, min(remaining, self._poll))


__all__ = [
    "BrowserExecutionError",
    "BrowserExecutionTimeout",
    "RemoteBrowserService",
]
