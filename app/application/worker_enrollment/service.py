"""Owner-only issuance and atomic one-use credential exchange."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.application.worker_enrollment.contracts import (
    EnrollmentActivation,
    EnrollmentCredentials,
    EnrollmentError,
    EnrollmentIssue,
    EnrollmentTicket,
)

if TYPE_CHECKING:
    from app.application.worker_enrollment.ports import WorkerEnrollmentPort

_TICKET_PREFIX: Final[str] = "pe1_"
_TICKET_TTL: Final[timedelta] = timedelta(minutes=5)
_ACTIVATION_TTL: Final[timedelta] = timedelta(hours=24)


class WorkerEnrollmentService:
    def __init__(self, repository: WorkerEnrollmentPort) -> None:
        self._repository = repository

    async def issue(
        self,
        command: EnrollmentIssue,
        *,
        now: datetime | None = None,
    ) -> EnrollmentTicket:
        if not command.is_primary_owner or command.owner_user_id <= 0:
            raise PermissionError("only the primary owner can issue enrollment")
        issued_at = _aware_utc(now)
        expires_at = issued_at + _TICKET_TTL
        plaintext = _TICKET_PREFIX + secrets.token_urlsafe(32)
        ledger_id = await self._repository.issue(
            ticket_hash=_digest(plaintext),
            owner_user_id=command.owner_user_id,
            expected_worker_id=command.expected_worker_id,
            issued_at=issued_at.isoformat(),
            expires_at=expires_at.isoformat(),
        )
        return EnrollmentTicket(
            ticket=plaintext,
            expires_at=expires_at,
            expected_worker_id=command.expected_worker_id,
            ledger_id=ledger_id,
        )

    async def exchange(
        self,
        ticket: str,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> EnrollmentCredentials:
        raw = str(ticket or "").strip()
        if not raw.startswith(_TICKET_PREFIX) or len(raw) < 40 or len(raw) > 96:
            raise EnrollmentError("invalid")
        llm_token = secrets.token_urlsafe(32)
        browser_token = secrets.token_urlsafe(32)
        exchanged_at = _aware_utc(now)
        activation_expires_at = exchanged_at + _ACTIVATION_TTL
        outcome, ledger_id = await self._repository.consume_to_pending(
            ticket_hash=_digest(raw),
            worker_id=worker_id,
            llm_token_hash=_digest(llm_token),
            browser_token_hash=_digest(browser_token),
            now_iso=exchanged_at.isoformat(),
            activation_expires_at=activation_expires_at.isoformat(),
        )
        if outcome != "consumed" or ledger_id is None:
            raise EnrollmentError(
                outcome,
                known_ticket=outcome != "invalid",
            )
        return EnrollmentCredentials(
            llm_worker_token=llm_token,
            browser_worker_token=browser_token,
            worker_id=worker_id,
            ledger_id=ledger_id,
            activation_expires_at=activation_expires_at,
        )

    async def activate(
        self,
        *,
        ledger_id: int,
        worker_id: str,
        llm_worker_token: str,
        browser_worker_token: str,
        now: datetime | None = None,
    ) -> EnrollmentActivation:
        if ledger_id <= 0:
            raise EnrollmentError("invalid")
        llm_token = _credential(llm_worker_token)
        browser_token = _credential(browser_worker_token)
        outcome, activated_at_raw = await self._repository.activate(
            ledger_id=ledger_id,
            worker_id=worker_id,
            llm_token_hash=_digest(llm_token),
            browser_token_hash=_digest(browser_token),
            now_iso=_aware_utc(now).isoformat(),
        )
        if outcome not in {"activated", "already_activated"} or not activated_at_raw:
            raise EnrollmentError(
                outcome,
                known_ticket=outcome != "invalid",
            )
        activated_at = datetime.fromisoformat(activated_at_raw)
        return EnrollmentActivation(
            worker_id=worker_id,
            ledger_id=ledger_id,
            activated_at=_aware_utc(activated_at),
            already_active=outcome == "already_activated",
        )

    async def status(self, *, now: datetime | None = None) -> dict[str, object]:
        return await self._repository.status(now_iso=_aware_utc(now).isoformat())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _credential(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) < 32 or len(raw) > 128:
        raise EnrollmentError("invalid")
    return raw


def _aware_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("enrollment clock must be timezone-aware")
    return current.astimezone(UTC)


__all__ = ["WorkerEnrollmentService"]
