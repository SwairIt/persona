"""SQLite implementation of the durable autowake repository port."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any, Final
from uuid import uuid4

from app.application.autowake.ports import (
    AutowakeStateError,
    EnqueueResult,
    IdempotencyConflict,
    OutboxItem,
)
from app.domains.autowake import (
    DeliveryDecision,
    DeliveryState,
    ProactiveContent,
    SourceScope,
)
from app.storage.db import get_connection, write_transaction

_SAFE_REJECTION_REASONS: Final = frozenset(
    {
        "group_context_marker",
        "secret_like_content",
        "unsafe_source_scope:external",
        "unsafe_source_scope:group",
        "unsafe_source_scope:secret",
    }
)


class SqliteAutowakeRepository:
    """Short atomic transitions; no network work happens under a DB lock."""

    async def policy_state(
        self,
        owner_user_id: int,
        *,
        now: datetime,
    ) -> DeliveryState:
        _require_owner(owner_user_id)
        _require_aware(now)
        local_start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
        local_end = local_start + timedelta(days=1)
        start_iso = _iso(local_start)
        end_iso = _iso(local_end)
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT COALESCE(SUM(
                           CASE
                               WHEN delivered_at>=? AND delivered_at<? THEN 1
                               ELSE 0
                           END
                       ), 0) AS delivered_today,
                       MAX(delivered_at) AS last_delivered_at
                  FROM autowake_outbox
                 WHERE owner_user_id=?
                   AND status='delivered'
                """,
                (start_iso, end_iso, owner_user_id),
            )
            row = await cursor.fetchone()
            quiet_cursor = await conn.execute(
                """
                SELECT MAX(end_hour) AS end_hour
                  FROM quiet_hours
                 WHERE weekday=?
                   AND start_hour<=?
                   AND end_hour>?
                """,
                (now.weekday(), now.hour, now.hour),
            )
            quiet = await quiet_cursor.fetchone()
        last = (
            _parse_datetime(row["last_delivered_at"])
            if row is not None and row["last_delivered_at"] is not None
            else None
        )
        quiet_until = None
        if quiet is not None and quiet["end_hour"] is not None:
            end_hour = int(quiet["end_hour"])
            if end_hour == 24:
                quiet_until = datetime.combine(
                    now.date() + timedelta(days=1),
                    time.min,
                    tzinfo=now.tzinfo,
                )
            else:
                quiet_until = now.replace(
                    hour=end_hour,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
        return DeliveryState(
            delivered_today=int(row["delivered_today"]) if row else 0,
            last_delivered_at=last,
            quiet_until=quiet_until,
        )

    async def enqueue(
        self,
        *,
        owner_user_id: int,
        content: ProactiveContent,
        decision: DeliveryDecision,
        fingerprint: str,
        max_attempts: int,
    ) -> EnqueueResult:
        _require_owner(owner_user_id)
        if len(fingerprint) != 64:
            raise ValueError("invalid content fingerprint")
        if decision.kind == "reject" and decision.reason not in _SAFE_REJECTION_REASONS:
            raise ValueError("invalid privacy rejection reason")
        if decision.kind != "reject" and decision.due_at is None:
            raise ValueError("accepted autowake must have a due_at")

        created_at = datetime.now(UTC)
        created_iso = _iso(created_at)
        accepted = decision.kind != "reject"
        async with write_transaction() as conn:
            if accepted:
                existing = await _event_by_key(
                    conn,
                    owner_user_id=owner_user_id,
                    idempotency_key=content.idempotency_key,
                )
                if existing is not None:
                    if str(existing["content_fingerprint"]) != fingerprint:
                        raise IdempotencyConflict(
                            "autowake idempotency key was reused for different content"
                        )
                    return await _existing_result(conn, existing)

            stored_key = (
                content.idempotency_key if accepted else f"rejected:{uuid4().hex}"
            )
            stored_fingerprint = fingerprint if accepted else "0" * 64
            event_cursor = await conn.execute(
                """
                INSERT INTO autowake_event(
                    owner_user_id, kind, source, source_scope, idempotency_key,
                    content_fingerprint, status, rejection_reason,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    owner_user_id,
                    content.kind,
                    content.source,
                    content.source_scope.value,
                    stored_key,
                    stored_fingerprint,
                    "queued" if accepted else "rejected",
                    None if accepted else decision.reason,
                    created_iso,
                    created_iso,
                ),
            )
            event_id = _last_id(event_cursor.lastrowid, "autowake event")
            if not accepted:
                # Privacy invariant: group/external/secret text is not stored in
                # either the message or outbox table.
                return EnqueueResult(
                    event_id=event_id,
                    outbox_id=None,
                    created=True,
                    accepted=False,
                    reason=decision.reason,
                    due_at=None,
                )

            due_at = decision.due_at
            if due_at is None:
                raise RuntimeError("accepted autowake lost due_at")
            _require_aware(due_at)
            session_cursor = await conn.execute(
                """
                INSERT INTO autowake_session(
                    owner_user_id, trigger_event_id, status, created_at
                ) VALUES(?,?,'queued',?)
                """,
                (owner_user_id, event_id, created_iso),
            )
            session_id = _last_id(session_cursor.lastrowid, "autowake session")
            message_cursor = await conn.execute(
                """
                INSERT INTO autowake_message(
                    session_id, role, source_scope, content, created_at
                ) VALUES(?,'assistant',?,?,?)
                """,
                (
                    session_id,
                    content.source_scope.value,
                    content.text.strip(),
                    created_iso,
                ),
            )
            message_id = _last_id(message_cursor.lastrowid, "autowake message")
            outbox_cursor = await conn.execute(
                """
                INSERT INTO autowake_outbox(
                    event_id, session_id, message_id, owner_user_id,
                    idempotency_key, channel, status, due_at, attempts,
                    max_attempts, defer_reason, created_at, updated_at
                ) VALUES(?,?,?,?,?,'telegram_owner_dm','pending',?,0,?,?,?,?)
                """,
                (
                    event_id,
                    session_id,
                    message_id,
                    owner_user_id,
                    content.idempotency_key,
                    _iso(due_at),
                    max_attempts,
                    decision.reason if decision.kind == "defer" else None,
                    created_iso,
                    created_iso,
                ),
            )
            outbox_id = _last_id(outbox_cursor.lastrowid, "autowake outbox")
        return EnqueueResult(
            event_id=event_id,
            outbox_id=outbox_id,
            created=True,
            accepted=True,
            reason=decision.reason,
            due_at=due_at,
        )

    async def claim_due(
        self,
        *,
        lease_owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> OutboxItem | None:
        if not lease_owner or len(lease_owner) > 96:
            raise ValueError("invalid lease owner")
        _require_aware(now)
        if not 15 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be in 15..300")
        now_iso = _iso(now)
        lease_until = _iso(now + timedelta(seconds=lease_seconds))
        async with write_transaction() as conn:
            await _recover_expired_leases(conn, now_iso)
            cursor = await conn.execute(
                """
                SELECT id
                  FROM autowake_outbox
                 WHERE status IN ('pending', 'retry')
                   AND due_at<=?
                   AND attempts<max_attempts
                 ORDER BY due_at ASC, id ASC
                 LIMIT 1
                """,
                (now_iso,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            outbox_id = int(row["id"])
            updated = await conn.execute(
                """
                UPDATE autowake_outbox
                   SET status='leased',
                       lease_owner=?,
                       lease_until=?,
                       attempt_started_at=NULL,
                       defer_reason=NULL,
                       updated_at=?
                 WHERE id=?
                   AND status IN ('pending', 'retry')
                   AND due_at<=?
                """,
                (
                    lease_owner,
                    lease_until,
                    now_iso,
                    outbox_id,
                    now_iso,
                ),
            )
            if updated.rowcount != 1:
                return None
            item_row = await _joined_outbox_row(conn, outbox_id)
        return _outbox_item(item_row)

    async def start_attempt(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        now: datetime,
    ) -> int:
        _require_aware(now)
        now_iso = _iso(now)
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE autowake_outbox
                   SET attempts=attempts+1,
                       attempt_started_at=?,
                       updated_at=?
                 WHERE id=?
                   AND status='leased'
                   AND lease_owner=?
                   AND lease_until>?
                   AND attempts<max_attempts
                """,
                (now_iso, now_iso, outbox_id, lease_owner, now_iso),
            )
            if cursor.rowcount != 1:
                raise AutowakeStateError("autowake lease is absent or expired")
            attempt_cursor = await conn.execute(
                "SELECT attempts FROM autowake_outbox WHERE id=?",
                (outbox_id,),
            )
            row = await attempt_cursor.fetchone()
            if row is None:
                raise AutowakeStateError("autowake outbox disappeared")
            return int(row["attempts"])

    async def defer(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        due_at: datetime,
        reason: str,
    ) -> None:
        _require_aware(due_at)
        if reason not in {"cooldown", "daily_cap", "quiet_hours"}:
            raise ValueError("invalid autowake deferral reason")
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE autowake_outbox
                   SET status='pending',
                       due_at=?,
                       lease_owner=NULL,
                       lease_until=NULL,
                       attempt_started_at=NULL,
                       defer_reason=?,
                       updated_at=?
                 WHERE id=? AND status='leased' AND lease_owner=?
                """,
                (
                    _iso(due_at),
                    reason,
                    _iso(datetime.now(UTC)),
                    outbox_id,
                    lease_owner,
                ),
            )
            _require_transition(cursor.rowcount, "defer")

    async def mark_delivered(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        delivered_at: datetime,
    ) -> None:
        _require_aware(delivered_at)
        delivered_iso = _iso(delivered_at)
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE autowake_outbox
                   SET status='delivered',
                       lease_owner=NULL,
                       lease_until=NULL,
                       attempt_started_at=NULL,
                       delivered_at=?,
                       last_error_code=NULL,
                       updated_at=?
                 WHERE id=? AND status='leased' AND lease_owner=?
                """,
                (delivered_iso, delivered_iso, outbox_id, lease_owner),
            )
            _require_transition(cursor.rowcount, "deliver")
            await _finish_parent_rows(
                conn,
                outbox_id=outbox_id,
                status="delivered",
                finished_at=delivered_iso,
            )

    async def mark_failed(
        self,
        outbox_id: int,
        *,
        lease_owner: str,
        failed_at: datetime,
        retry_at: datetime,
        error_code: str,
    ) -> str:
        _require_aware(failed_at)
        _require_aware(retry_at)
        clean_error = _clean_error_code(error_code)
        async with write_transaction() as conn:
            state_cursor = await conn.execute(
                """
                SELECT attempts, max_attempts
                  FROM autowake_outbox
                 WHERE id=? AND status='leased' AND lease_owner=?
                """,
                (outbox_id, lease_owner),
            )
            state = await state_cursor.fetchone()
            if state is None:
                raise AutowakeStateError("cannot fail an unowned autowake lease")
            terminal = int(state["attempts"]) >= int(state["max_attempts"])
            status = "dead" if terminal else "retry"
            cursor = await conn.execute(
                """
                UPDATE autowake_outbox
                   SET status=?,
                       due_at=?,
                       lease_owner=NULL,
                       lease_until=NULL,
                       attempt_started_at=NULL,
                       last_error_code=?,
                       updated_at=?
                 WHERE id=? AND status='leased' AND lease_owner=?
                """,
                (
                    status,
                    _iso(retry_at),
                    clean_error,
                    _iso(failed_at),
                    outbox_id,
                    lease_owner,
                ),
            )
            _require_transition(cursor.rowcount, "fail")
            if terminal:
                await _finish_parent_rows(
                    conn,
                    outbox_id=outbox_id,
                    status="dead",
                    finished_at=_iso(failed_at),
                )
        return status


async def _event_by_key(
    conn: Any,
    *,
    owner_user_id: int,
    idempotency_key: str,
) -> Any | None:
    cursor = await conn.execute(
        """
        SELECT id, content_fingerprint, status, rejection_reason
          FROM autowake_event
         WHERE owner_user_id=? AND idempotency_key=?
        """,
        (owner_user_id, idempotency_key),
    )
    return await cursor.fetchone()


async def _existing_result(conn: Any, event: Any) -> EnqueueResult:
    cursor = await conn.execute(
        "SELECT id, due_at FROM autowake_outbox WHERE event_id=?",
        (int(event["id"]),),
    )
    outbox = await cursor.fetchone()
    accepted = outbox is not None
    return EnqueueResult(
        event_id=int(event["id"]),
        outbox_id=int(outbox["id"]) if outbox else None,
        created=False,
        accepted=accepted,
        reason=("duplicate" if accepted else str(event["rejection_reason"] or "rejected")),
        due_at=_parse_datetime(outbox["due_at"]) if outbox else None,
    )


async def _joined_outbox_row(conn: Any, outbox_id: int) -> Any:
    cursor = await conn.execute(
        """
        SELECT o.id, o.event_id, o.session_id, o.message_id, o.owner_user_id,
               o.due_at, o.attempts, o.max_attempts, o.lease_owner,
               e.kind, e.source, e.source_scope, o.idempotency_key,
               m.content
          FROM autowake_outbox AS o
          JOIN autowake_event AS e ON e.id=o.event_id
          JOIN autowake_message AS m ON m.id=o.message_id
         WHERE o.id=?
        """,
        (outbox_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise AutowakeStateError("claimed autowake row disappeared")
    return row


def _outbox_item(row: Any) -> OutboxItem:
    lease_owner = row["lease_owner"]
    if lease_owner is None:
        raise AutowakeStateError("claimed autowake row has no lease owner")
    return OutboxItem(
        id=int(row["id"]),
        event_id=int(row["event_id"]),
        session_id=int(row["session_id"]),
        message_id=int(row["message_id"]),
        owner_user_id=int(row["owner_user_id"]),
        content=ProactiveContent(
            kind=str(row["kind"]),
            source=str(row["source"]),
            source_scope=SourceScope(str(row["source_scope"])),
            text=str(row["content"]),
            idempotency_key=str(row["idempotency_key"]),
        ),
        due_at=_parse_datetime(row["due_at"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        lease_owner=str(lease_owner),
    )


async def _recover_expired_leases(conn: Any, now_iso: str) -> None:
    cursor = await conn.execute(
        """
        SELECT id, attempts, max_attempts, attempt_started_at
          FROM autowake_outbox
         WHERE status='leased' AND lease_until<=?
         ORDER BY id
        """,
        (now_iso,),
    )
    for row in await cursor.fetchall():
        outbox_id = int(row["id"])
        attempts = int(row["attempts"])
        if row["attempt_started_at"] is None:
            attempts += 1
        terminal = attempts >= int(row["max_attempts"])
        status = "dead" if terminal else "retry"
        await conn.execute(
            """
            UPDATE autowake_outbox
               SET status=?, attempts=?, due_at=?,
                   lease_owner=NULL, lease_until=NULL,
                   attempt_started_at=NULL,
                   last_error_code='lease_expired', updated_at=?
             WHERE id=? AND status='leased' AND lease_until<=?
            """,
            (status, attempts, now_iso, now_iso, outbox_id, now_iso),
        )
        if terminal:
            await _finish_parent_rows(
                conn,
                outbox_id=outbox_id,
                status="dead",
                finished_at=now_iso,
            )


async def _finish_parent_rows(
    conn: Any,
    *,
    outbox_id: int,
    status: str,
    finished_at: str,
) -> None:
    cursor = await conn.execute(
        "SELECT event_id, session_id FROM autowake_outbox WHERE id=?",
        (outbox_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise AutowakeStateError("autowake parent rows disappeared")
    await conn.execute(
        "UPDATE autowake_event SET status=?, updated_at=? WHERE id=?",
        (status, finished_at, int(row["event_id"])),
    )
    await conn.execute(
        "UPDATE autowake_session SET status=?, finished_at=? WHERE id=?",
        (status, finished_at, int(row["session_id"])),
    )


def _last_id(value: int | None, label: str) -> int:
    if value is None:
        raise RuntimeError(f"{label} insert returned no id")
    return int(value)


def _require_transition(rowcount: int, operation: str) -> None:
    if rowcount != 1:
        raise AutowakeStateError(f"cannot {operation} unowned autowake lease")


def _require_owner(owner_user_id: int) -> None:
    if owner_user_id <= 0:
        raise ValueError("owner_user_id must be positive")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("autowake datetimes must be timezone-aware")


def _iso(value: datetime) -> str:
    _require_aware(value)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_datetime(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _clean_error_code(value: str) -> str:
    clean = "".join(ch for ch in value if ch.isalnum() or ch in "._-")
    return (clean or "transport_error")[:80]


__all__ = ["SqliteAutowakeRepository"]
