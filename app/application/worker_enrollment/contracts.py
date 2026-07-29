"""Transport-neutral enrollment contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


class EnrollmentError(RuntimeError):
    """A ticket cannot be issued or exchanged."""

    def __init__(self, reason: str, *, known_ticket: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.known_ticket = known_ticket


@dataclass(frozen=True, slots=True)
class EnrollmentIssue:
    owner_user_id: int
    is_primary_owner: bool
    expected_worker_id: str | None = None


@dataclass(frozen=True, slots=True)
class EnrollmentTicket:
    ticket: str = field(repr=False)
    expires_at: datetime
    expected_worker_id: str | None
    ledger_id: int


@dataclass(frozen=True, slots=True)
class EnrollmentCredentials:
    llm_worker_token: str = field(repr=False)
    browser_worker_token: str = field(repr=False)
    worker_id: str
    ledger_id: int
    activation_expires_at: datetime


@dataclass(frozen=True, slots=True)
class EnrollmentActivation:
    worker_id: str
    ledger_id: int
    activated_at: datetime
    already_active: bool


__all__ = [
    "EnrollmentActivation",
    "EnrollmentCredentials",
    "EnrollmentError",
    "EnrollmentIssue",
    "EnrollmentTicket",
]
