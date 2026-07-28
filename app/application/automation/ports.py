"""Ports used by the remote browser application service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.application.automation.contracts import BrowserCommand, BrowserJob


class RemoteBrowserJobPort(Protocol):
    async def enqueue(self, command: BrowserCommand) -> int: ...

    async def get(self, job_id: int) -> BrowserJob | None: ...

    async def wait_for_change(self, job_id: int, timeout: float) -> bool: ...

    async def cancel(self, job_id: int, reason: str) -> bool: ...

    async def scrub_sensitive(self, job_id: int) -> None: ...

    def forget(self, job_id: int) -> None: ...


__all__ = ["RemoteBrowserJobPort"]
