"""Persistence port for worker enrollment."""

from __future__ import annotations

from typing import Protocol


class WorkerEnrollmentPort(Protocol):
    async def issue(
        self,
        *,
        ticket_hash: str,
        owner_user_id: int,
        expected_worker_id: str | None,
        issued_at: str,
        expires_at: str,
    ) -> int: ...

    async def consume_to_pending(
        self,
        *,
        ticket_hash: str,
        worker_id: str,
        llm_token_hash: str,
        browser_token_hash: str,
        now_iso: str,
        activation_expires_at: str,
    ) -> tuple[str, int | None]: ...

    async def activate(
        self,
        *,
        ledger_id: int,
        worker_id: str,
        llm_token_hash: str,
        browser_token_hash: str,
        now_iso: str,
    ) -> tuple[str, str | None]: ...

    async def status(self, *, now_iso: str) -> dict[str, object]: ...


__all__ = ["WorkerEnrollmentPort"]
