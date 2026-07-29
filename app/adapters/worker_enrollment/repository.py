"""Atomic SQLite ledger for short-lived worker enrollment tickets."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from app.storage.db import get_connection, write_transaction

if TYPE_CHECKING:
    import sqlite3

_CAPABILITY = "llm+browser"
_KV_LLM_TOKEN_HASH = "llm_worker_token_hash"  # noqa: S105
_KV_BROWSER_TOKEN_HASH = "remote_browser_worker_token_hash"  # noqa: S105


class SqliteWorkerEnrollment:
    async def issue(
        self,
        *,
        ticket_hash: str,
        owner_user_id: int,
        expected_worker_id: str | None,
        issued_at: str,
        expires_at: str,
    ) -> int:
        async with write_transaction() as conn:
            await conn.execute(
                """
                UPDATE worker_enrollment_ticket
                   SET status='expired', revoked_at=?
                 WHERE status='issued' AND expires_at<=?
                """,
                (issued_at, issued_at),
            )
            await conn.execute(
                """
                UPDATE worker_enrollment_ticket
                   SET status='revoked', revoked_at=?
                 WHERE owner_user_id=? AND capability=? AND status='issued'
                """,
                (issued_at, owner_user_id, _CAPABILITY),
            )
            await conn.execute(
                """
                UPDATE worker_enrollment_ticket
                   SET status='revoked', revoked_at=?,
                       consumed_at=NULL, consumed_worker_id=NULL,
                       pending_llm_token_hash=NULL,
                       pending_browser_token_hash=NULL,
                       activation_expires_at=NULL
                 WHERE owner_user_id=? AND capability=? AND status='consumed'
                   AND activated_at IS NULL
                   AND pending_llm_token_hash IS NOT NULL
                   AND pending_browser_token_hash IS NOT NULL
                """,
                (issued_at, owner_user_id, _CAPABILITY),
            )
            cursor = await conn.execute(
                """
                INSERT INTO worker_enrollment_ticket (
                    ticket_hash, owner_user_id, capability,
                    expected_worker_id, issued_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_hash,
                    owner_user_id,
                    _CAPABILITY,
                    expected_worker_id,
                    issued_at,
                    expires_at,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("worker enrollment ticket insert returned no id")
            return int(cursor.lastrowid)

    async def consume_to_pending(
        self,
        *,
        ticket_hash: str,
        worker_id: str,
        llm_token_hash: str,
        browser_token_hash: str,
        now_iso: str,
        activation_expires_at: str,
    ) -> tuple[str, int | None]:
        # Unknown/replayed/mismatched tickets must not take SQLite's global
        # writer lock. The public route rate-limits before this read.
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, status, expires_at, expected_worker_id
                  FROM worker_enrollment_ticket
                 WHERE ticket_hash=? AND capability=?
                """,
                (ticket_hash, _CAPABILITY),
            )
            row = await cursor.fetchone()
        outcome, ledger_id = _exchange_outcome(row, worker_id, now_iso)
        if outcome != "ready" or ledger_id is None:
            return outcome, ledger_id

        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT id, status, expires_at, expected_worker_id
                  FROM worker_enrollment_ticket
                 WHERE id=? AND ticket_hash=? AND capability=?
                """,
                (ledger_id, ticket_hash, _CAPABILITY),
            )
            current = await cursor.fetchone()
            outcome, current_id = _exchange_outcome(current, worker_id, now_iso)
            if outcome != "ready" or current_id != ledger_id:
                return outcome, current_id
            consumed = await conn.execute(
                """
                UPDATE worker_enrollment_ticket
                   SET status='consumed', consumed_at=?,
                       consumed_worker_id=?,
                       pending_llm_token_hash=?,
                       pending_browser_token_hash=?,
                       activation_expires_at=?
                 WHERE id=? AND status='issued' AND expires_at>?
                 RETURNING id
                """,
                (
                    now_iso,
                    worker_id,
                    llm_token_hash,
                    browser_token_hash,
                    activation_expires_at,
                    ledger_id,
                    now_iso,
                ),
            )
            if await consumed.fetchone() is None:
                return "replayed", ledger_id
            return "consumed", ledger_id

    async def activate(
        self,
        *,
        ledger_id: int,
        worker_id: str,
        llm_token_hash: str,
        browser_token_hash: str,
        now_iso: str,
    ) -> tuple[str, str | None]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, status, activation_expires_at, consumed_worker_id,
                       pending_llm_token_hash, pending_browser_token_hash,
                       activated_at
                  FROM worker_enrollment_ticket
                 WHERE id=? AND capability=?
                """,
                (ledger_id, _CAPABILITY),
            )
            row = await cursor.fetchone()
        outcome, activated_at = _activation_outcome(
            row,
            worker_id=worker_id,
            llm_token_hash=llm_token_hash,
            browser_token_hash=browser_token_hash,
            now_iso=now_iso,
        )
        if outcome != "ready":
            return outcome, activated_at

        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT id, status, activation_expires_at, consumed_worker_id,
                       pending_llm_token_hash, pending_browser_token_hash,
                       activated_at
                  FROM worker_enrollment_ticket
                 WHERE id=? AND capability=?
                """,
                (ledger_id, _CAPABILITY),
            )
            current = await cursor.fetchone()
            outcome, activated_at = _activation_outcome(
                current,
                worker_id=worker_id,
                llm_token_hash=llm_token_hash,
                browser_token_hash=browser_token_hash,
                now_iso=now_iso,
            )
            if outcome != "ready":
                return outcome, activated_at
            activated = await conn.execute(
                """
                UPDATE worker_enrollment_ticket
                   SET activated_at=?
                 WHERE id=? AND status='consumed' AND activated_at IS NULL
                 RETURNING activated_at
                """,
                (now_iso, ledger_id),
            )
            activated_row = await activated.fetchone()
            if activated_row is None:
                raise RuntimeError("worker enrollment activation lost its ledger row")
            for key, digest in (
                (_KV_LLM_TOKEN_HASH, llm_token_hash),
                (_KV_BROWSER_TOKEN_HASH, browser_token_hash),
            ):
                await conn.execute(
                    """
                    INSERT INTO kv_settings(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        updated_at=excluded.updated_at
                    """,
                    (key, digest, now_iso),
                )
            return "activated", str(activated_row["activated_at"])

    async def status(self, *, now_iso: str) -> dict[str, object]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT
                    SUM(
                        CASE WHEN status='issued' AND expires_at>? THEN 1 ELSE 0 END
                    ) AS active,
                    SUM(
                        CASE WHEN status='consumed' AND activated_at IS NULL
                                  AND activation_expires_at>? THEN 1 ELSE 0 END
                    ) AS pending,
                    SUM(
                        CASE WHEN status='consumed' AND activated_at IS NOT NULL
                             THEN 1 ELSE 0 END
                    ) AS activated,
                    MAX(issued_at) AS last_issued_at,
                    MAX(consumed_at) AS last_consumed_at,
                    (
                        SELECT consumed_worker_id
                          FROM worker_enrollment_ticket
                         WHERE status='consumed'
                         ORDER BY consumed_at DESC, id DESC
                         LIMIT 1
                    ) AS last_worker_id
                  FROM worker_enrollment_ticket
                """,
                (now_iso, now_iso),
            )
            row = await cursor.fetchone()
        return {
            "active_tickets": int(row["active"] or 0) if row is not None else 0,
            "pending_activations": (
                int(row["pending"] or 0) if row is not None else 0
            ),
            "activated_enrollments": (
                int(row["activated"] or 0) if row is not None else 0
            ),
            "last_issued_at": row["last_issued_at"] if row is not None else None,
            "last_consumed_at": row["last_consumed_at"] if row is not None else None,
            "last_worker_id": row["last_worker_id"] if row is not None else None,
        }


def _exchange_outcome(
    row: sqlite3.Row | None,
    worker_id: str,
    now_iso: str,
) -> tuple[str, int | None]:
    if row is None:
        return "invalid", None
    ledger_id = int(row["id"])
    status = str(row["status"])
    if status == "consumed":
        return "replayed", ledger_id
    if status != "issued":
        return status, ledger_id
    if str(row["expires_at"]) <= now_iso:
        return "expired", ledger_id
    expected = row["expected_worker_id"]
    if expected is not None and str(expected) != worker_id:
        return "worker_mismatch", ledger_id
    return "ready", ledger_id


def _activation_outcome(
    row: sqlite3.Row | None,
    *,
    worker_id: str,
    llm_token_hash: str,
    browser_token_hash: str,
    now_iso: str,
) -> tuple[str, str | None]:
    if row is None:
        return "invalid", None
    if str(row["status"]) != "consumed":
        return "not_pending", None
    expected_worker = str(row["consumed_worker_id"] or "")
    stored_llm = str(row["pending_llm_token_hash"] or "")
    stored_browser = str(row["pending_browser_token_hash"] or "")
    if (
        expected_worker != worker_id
        or not stored_llm
        or not stored_browser
        or not hmac.compare_digest(stored_llm, llm_token_hash)
        or not hmac.compare_digest(stored_browser, browser_token_hash)
    ):
        return "invalid_credentials", None
    activated_at = row["activated_at"]
    if activated_at is not None:
        return "already_activated", str(activated_at)
    if str(row["activation_expires_at"] or "") <= now_iso:
        return "expired", None
    return "ready", None


__all__ = ["SqliteWorkerEnrollment"]
