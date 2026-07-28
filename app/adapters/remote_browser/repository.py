"""Durable SQLite adapter for outbound-only browser workers."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Final

from app.application.automation.contracts import (
    BrowserAction,
    BrowserCommand,
    BrowserJob,
)
from app.storage.db import get_connection, write_transaction

MAX_RESULT_BYTES: Final[int] = 2 * 1024 * 1024
MAX_ERROR_CHARS: Final[int] = 2_000
MAX_WORKER_ID_CHARS: Final[int] = 96
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_MAINTENANCE_INTERVAL_SECONDS = 30.0
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_PENDING_TIMEOUT_SECONDS = 5 * 60
_RESULT_SCRUB_GRACE_SECONDS = 90


class RemoteBrowserJobStateError(RuntimeError):
    """A worker attempted an invalid or foreign job transition."""


@dataclass(slots=True)
class _Maintenance:
    last_run: float = 0.0
    lock: asyncio.Lock | None = None


_maintenance = _Maintenance()
_job_events: dict[int, asyncio.Event] = {}
_pending_event = asyncio.Event()


def validate_worker_id(worker_id: str) -> str:
    normalized = str(worker_id or "").strip()
    if not _WORKER_ID_RE.fullmatch(normalized):
        raise ValueError(
            f"worker_id must match {_WORKER_ID_RE.pattern} "
            f"and be <= {MAX_WORKER_ID_CHARS} characters"
        )
    return normalized


class SqliteRemoteBrowserJobs:
    """Short-transaction job repository with worker-bound browser sessions."""

    async def touch_worker(self, worker_id: str) -> None:
        worker = validate_worker_id(worker_id)
        async with write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO remote_browser_worker_presence(worker_id, last_seen)
                VALUES (?, datetime('now'))
                ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen
                """,
                (worker,),
            )

    async def worker_status(self) -> dict[str, Any]:
        """Return bounded worker presence for owner-only diagnostics."""
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT worker_id, last_seen,
                       last_seen >= datetime('now', '-90 seconds') AS online
                  FROM remote_browser_worker_presence
                 ORDER BY last_seen DESC
                 LIMIT 8
                """
            )
            rows = await cursor.fetchall()
        workers = [
            {
                "worker_id": str(row["worker_id"]),
                "last_seen": str(row["last_seen"]),
                "online": bool(row["online"]),
            }
            for row in rows
        ]
        return {
            "online": any(worker["online"] for worker in workers),
            "workers": workers,
        }

    async def enqueue(self, command: BrowserCommand) -> int:
        payload = json.dumps(
            command.action.arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO remote_browser_session
                    (owner_user_id, conversation_id, state)
                VALUES (?, ?, 'open')
                ON CONFLICT(owner_user_id, conversation_id) DO UPDATE SET
                    state=CASE
                        WHEN remote_browser_session.state='closed' THEN 'open'
                        ELSE remote_browser_session.state
                    END,
                    updated_at=datetime('now')
                """,
                (command.owner_user_id, command.session_id),
            )
            session_cur = await conn.execute(
                """
                SELECT id, assigned_worker_id
                  FROM remote_browser_session
                 WHERE owner_user_id=? AND conversation_id=?
                """,
                (command.owner_user_id, command.session_id),
            )
            session = await session_cur.fetchone()
            if session is None:
                raise RuntimeError("remote browser session was not created")

            existing_cur = await conn.execute(
                """
                SELECT id, browser_session_id, action, payload
                  FROM remote_browser_job
                 WHERE owner_user_id=? AND correlation_id=?
                """,
                (command.owner_user_id, command.correlation_id),
            )
            existing = await existing_cur.fetchone()
            if existing is not None:
                if (
                    int(existing["browser_session_id"]) != int(session["id"])
                    or existing["action"] != command.action.name
                    or existing["payload"] != payload
                ):
                    raise RemoteBrowserJobStateError(
                        "correlation_id was already used for a different browser action"
                    )
                return int(existing["id"])

            cursor = await conn.execute(
                """
                INSERT INTO remote_browser_job (
                    browser_session_id, owner_user_id, conversation_id,
                    correlation_id, action, payload, target_worker_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(session["id"]),
                    command.owner_user_id,
                    command.session_id,
                    command.correlation_id,
                    command.action.name,
                    payload,
                    session["assigned_worker_id"],
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("remote browser job INSERT returned no id")
            job_id = int(cursor.lastrowid)
        _event(job_id)
        _pending_event.set()
        return job_id

    async def get(self, job_id: int) -> BrowserJob | None:
        # Clear before the DB snapshot. A concurrent transition after this
        # point sets the event again, so get -> wait cannot lose a wakeup.
        _event(job_id).clear()
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT j.*, s.owner_user_id AS session_owner_user_id,
                       s.last_url AS session_last_url
                  FROM remote_browser_job j
                  JOIN remote_browser_session s ON s.id=j.browser_session_id
                 WHERE j.id=?
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
        return _job_from_row(row)

    async def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 90,
    ) -> BrowserJob | None:
        worker = validate_worker_id(worker_id)
        await self._maybe_maintain()
        lease_modifier = f"+{max(15, min(int(lease_seconds), 300))} seconds"
        _pending_event.clear()
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT j.id, j.browser_session_id
                  FROM remote_browser_job j
                  JOIN remote_browser_session s ON s.id=j.browser_session_id
                 WHERE j.status='pending'
                   AND j.cancel_requested=0
                   AND (j.target_worker_id IS NULL OR j.target_worker_id=?)
                   AND (s.assigned_worker_id IS NULL OR s.assigned_worker_id=?)
                   AND s.active_job_id IS NULL
                 ORDER BY j.id
                 LIMIT 1
                """,
                (worker, worker),
            )
            candidate = await cursor.fetchone()
            if candidate is None:
                return None
            job_id = int(candidate["id"])
            session_id = int(candidate["browser_session_id"])
            session_update = await conn.execute(
                """
                UPDATE remote_browser_session
                   SET assigned_worker_id=COALESCE(assigned_worker_id, ?),
                       active_job_id=?,
                       state='open',
                       updated_at=datetime('now')
                 WHERE id=?
                   AND active_job_id IS NULL
                   AND (assigned_worker_id IS NULL OR assigned_worker_id=?)
                """,
                (worker, job_id, session_id, worker),
            )
            if session_update.rowcount != 1:
                return None
            job_update = await conn.execute(
                """
                UPDATE remote_browser_job
                   SET status='claimed',
                       worker_id=?,
                       target_worker_id=?,
                       claimed_at=datetime('now'),
                       lease_until=datetime('now', ?)
                 WHERE id=? AND status='pending' AND cancel_requested=0
                 RETURNING id
                """,
                (worker, worker, lease_modifier, job_id),
            )
            updated = await job_update.fetchone()
            if updated is None:
                await conn.execute(
                    """
                    UPDATE remote_browser_session
                       SET active_job_id=NULL, updated_at=datetime('now')
                     WHERE id=? AND active_job_id=?
                    """,
                    (session_id, job_id),
                )
                return None
            claimed_cur = await conn.execute(
                """
                SELECT j.*, s.last_url AS session_last_url
                  FROM remote_browser_job j
                  JOIN remote_browser_session s ON s.id=j.browser_session_id
                 WHERE j.id=?
                """,
                (job_id,),
            )
            row = await claimed_cur.fetchone()
            if row is None:
                raise RuntimeError("claimed remote browser job disappeared")
        return _job_from_row(row)

    async def heartbeat(
        self,
        job_id: int,
        worker_id: str,
        *,
        lease_seconds: int = 90,
    ) -> bool:
        """Extend a lease and return whether cancellation was requested."""
        worker = validate_worker_id(worker_id)
        lease_modifier = f"+{max(15, min(int(lease_seconds), 300))} seconds"
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE remote_browser_job
                   SET lease_until=datetime('now', ?)
                 WHERE id=? AND status='claimed' AND worker_id=?
                 RETURNING cancel_requested
                """,
                (lease_modifier, job_id, worker),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RemoteBrowserJobStateError(
                f"browser job {job_id} is not leased by worker {worker}"
            )
        return bool(row["cancel_requested"])

    async def finish(
        self,
        job_id: int,
        worker_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        worker = validate_worker_id(worker_id)
        result_json = (
            json.dumps(
                result or {},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            if error is None
            else None
        )
        if result_json is not None and len(result_json.encode("utf-8")) > MAX_RESULT_BYTES:
            raise ValueError(f"browser result exceeds {MAX_RESULT_BYTES} bytes")
        clean_error = (
            (str(error).strip()[:MAX_ERROR_CHARS] or "remote browser worker error")
            if error is not None
            else None
        )

        async with write_transaction() as conn:
            current_cur = await conn.execute(
                """
                SELECT browser_session_id, action, cancel_requested
                  FROM remote_browser_job
                 WHERE id=? AND status='claimed' AND worker_id=?
                """,
                (job_id, worker),
            )
            current = await current_cur.fetchone()
            if current is None:
                raise RemoteBrowserJobStateError(
                    f"browser job {job_id} is not leased by worker {worker}"
                )
            cancelled = bool(current["cancel_requested"])
            status = "cancelled" if cancelled else ("error" if clean_error else "done")
            final_error = (
                "cancelled by server" if cancelled else clean_error
            )
            final_result = None if cancelled or status == "error" else result_json
            await conn.execute(
                """
                UPDATE remote_browser_job
                   SET status=?, result=?, error=?, finished_at=datetime('now'),
                       lease_until=NULL
                 WHERE id=? AND status='claimed' AND worker_id=?
                """,
                (status, final_result, final_error, job_id, worker),
            )
            session_state = "closed" if current["action"] == "close" else (
                "error" if status == "error" else "open"
            )
            last_url = ""
            if result and isinstance(result.get("url"), str):
                last_url = str(result["url"])[:2_048]
            await conn.execute(
                """
                UPDATE remote_browser_session
                   SET active_job_id=NULL,
                       state=?,
                       last_url=CASE WHEN ?='' THEN last_url ELSE ? END,
                       assigned_worker_id=CASE WHEN ?='closed' THEN NULL
                                               ELSE assigned_worker_id END,
                       updated_at=datetime('now')
                 WHERE id=? AND active_job_id=?
                """,
                (
                    session_state,
                    last_url,
                    last_url,
                    session_state,
                    int(current["browser_session_id"]),
                    job_id,
                ),
            )
        _signal(job_id)
        _pending_event.set()
        return status

    async def cancel(self, job_id: int, reason: str) -> bool:
        clean_reason = (str(reason or "cancelled")[:MAX_ERROR_CHARS]) or "cancelled"
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT browser_session_id, status, action
                  FROM remote_browser_job
                 WHERE id=? AND status IN ('pending', 'claimed')
                """,
                (job_id,),
            )
            current = await cursor.fetchone()
            if current is None:
                return False
            redacted_payload = _redacted_payload(str(current["action"]))
            if current["status"] == "pending":
                await conn.execute(
                    """
                    UPDATE remote_browser_job
                       SET status='cancelled', cancel_requested=1, error=?,
                           finished_at=datetime('now'), payload=?, result=NULL
                     WHERE id=? AND status='pending'
                    """,
                    (clean_reason, redacted_payload, job_id),
                )
            else:
                # Keep the session single-flight until the worker acknowledges
                # cancellation or loses its lease.
                await conn.execute(
                    """
                    UPDATE remote_browser_job
                       SET cancel_requested=1, error=?, payload=?, result=NULL
                     WHERE id=? AND status='claimed'
                    """,
                    (clean_reason, redacted_payload, job_id),
                )
        _signal(job_id)
        return True

    async def wait_for_change(self, job_id: int, timeout: float) -> bool:
        try:
            await asyncio.wait_for(_event(job_id).wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return False
        return True

    async def wait_for_pending(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(_pending_event.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return False
        return True

    async def scrub_sensitive(self, job_id: int) -> None:
        """Erase typed text, URLs, page text and screenshots after consumption."""
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT action
                  FROM remote_browser_job
                 WHERE id=?
                   AND (
                       status IN ('done', 'error', 'cancelled')
                       OR cancel_requested=1
                   )
                """,
                (job_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            await conn.execute(
                """
                UPDATE remote_browser_job
                   SET payload=?, result=NULL
                 WHERE id=?
                """,
                (_redacted_payload(str(row["action"])), job_id),
            )

    def forget(self, job_id: int) -> None:
        _job_events.pop(job_id, None)

    async def maintain(
        self,
        *,
        pending_timeout_seconds: int = _PENDING_TIMEOUT_SECONDS,
        retention_seconds: int = _TERMINAL_RETENTION_SECONDS,
    ) -> dict[str, int]:
        pending_modifier = f"-{max(1, int(pending_timeout_seconds))} seconds"
        retention_modifier = f"-{max(1, int(retention_seconds))} seconds"
        scrub_modifier = f"-{_RESULT_SCRUB_GRACE_SECONDS} seconds"
        async with write_transaction() as conn:
            expired_cur = await conn.execute(
                """
                SELECT id, browser_session_id, cancel_requested, action
                  FROM remote_browser_job
                 WHERE status='claimed'
                   AND lease_until < datetime('now')
                """
            )
            expired = list(await expired_cur.fetchall())
            for row in expired:
                status = "cancelled" if row["cancel_requested"] else "error"
                error = (
                    "cancelled by server"
                    if row["cancel_requested"]
                    else "remote browser worker lost its lease"
                )
                await conn.execute(
                    """
                    UPDATE remote_browser_job
                       SET status=?, error=?, finished_at=datetime('now'),
                           lease_until=NULL, payload=?, result=NULL
                     WHERE id=? AND status='claimed'
                    """,
                    (
                        status,
                        error,
                        _redacted_payload(str(row["action"])),
                        int(row["id"]),
                    ),
                )
                await conn.execute(
                    """
                    UPDATE remote_browser_session
                       SET active_job_id=NULL, state=?, updated_at=datetime('now')
                     WHERE id=? AND active_job_id=?
                    """,
                    (
                        "error" if status == "error" else "open",
                        int(row["browser_session_id"]),
                        int(row["id"]),
                    ),
                )

            pending_cur = await conn.execute(
                """
                SELECT id, action FROM remote_browser_job
                 WHERE status='pending' AND created_at < datetime('now', ?)
                """,
                (pending_modifier,),
            )
            pending = list(await pending_cur.fetchall())
            for row in pending:
                await conn.execute(
                    """
                    UPDATE remote_browser_job
                       SET status='error',
                           error='remote browser worker unavailable',
                           finished_at=datetime('now'), payload=?, result=NULL
                     WHERE id=? AND status='pending'
                    """,
                    (
                        _redacted_payload(str(row["action"])),
                        int(row["id"]),
                    ),
                )

            terminal_cur = await conn.execute(
                """
                SELECT id, action FROM remote_browser_job
                 WHERE status IN ('error', 'cancelled')
                    OR (
                        status='done'
                        AND finished_at < datetime('now', ?)
                    )
                """,
                (scrub_modifier,),
            )
            terminal_rows = list(await terminal_cur.fetchall())
            for row in terminal_rows:
                await conn.execute(
                    """
                    UPDATE remote_browser_job
                       SET payload=?, result=NULL
                     WHERE id=?
                    """,
                    (
                        _redacted_payload(str(row["action"])),
                        int(row["id"]),
                    ),
                )

            terminal_cur = await conn.execute(
                """
                SELECT id FROM remote_browser_job
                 WHERE status IN ('done', 'error', 'cancelled')
                   AND finished_at < datetime('now', ?)
                """,
                (retention_modifier,),
            )
            terminal = list(await terminal_cur.fetchall())
            await conn.execute(
                """
                DELETE FROM remote_browser_job
                 WHERE status IN ('done', 'error', 'cancelled')
                   AND finished_at < datetime('now', ?)
                """,
                (retention_modifier,),
            )
            await conn.execute(
                """
                DELETE FROM remote_browser_worker_presence
                 WHERE last_seen < datetime('now', '-7 days')
                """
            )
        for row in (*expired, *pending):
            _signal(int(row["id"]))
        for row in terminal:
            self.forget(int(row["id"]))
        if expired:
            _pending_event.set()
        return {
            "leases_expired": len(expired),
            "pending_failed": len(pending),
            "terminal_deleted": len(terminal),
        }

    async def _maybe_maintain(self) -> None:
        now = time.monotonic()
        if now - _maintenance.last_run < _MAINTENANCE_INTERVAL_SECONDS:
            return
        if _maintenance.lock is None:
            _maintenance.lock = asyncio.Lock()
        async with _maintenance.lock:
            now = time.monotonic()
            if now - _maintenance.last_run < _MAINTENANCE_INTERVAL_SECONDS:
                return
            await self.maintain()
            _maintenance.last_run = now


def _event(job_id: int) -> asyncio.Event:
    event = _job_events.get(job_id)
    if event is None:
        event = asyncio.Event()
        _job_events[job_id] = event
    return event


def _signal(job_id: int) -> None:
    event = _job_events.get(job_id)
    if event is not None:
        event.set()


def _job_from_row(row: Any | None) -> BrowserJob | None:
    if row is None:
        return None
    try:
        arguments = json.loads(row["payload"]) if row["payload"] else {}
    except (TypeError, ValueError):
        arguments = {}
    action = BrowserAction.parse(str(row["action"]), arguments)
    result: dict[str, Any] | None = None
    if row["result"]:
        try:
            parsed = json.loads(row["result"])
            result = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            result = {}
    owner_user_id = int(row["owner_user_id"])
    conversation_id = int(row["conversation_id"])
    return BrowserJob(
        id=int(row["id"]),
        owner_user_id=owner_user_id,
        session_id=conversation_id,
        action=action,
        status=str(row["status"]),
        worker_id=str(row["worker_id"]) if row["worker_id"] else None,
        result=result,
        error=str(row["error"]) if row["error"] else None,
        cancel_requested=bool(row["cancel_requested"]),
        profile_key=f"owner-{owner_user_id}-session-{conversation_id}",
        resume_url=(
            str(row["session_last_url"])
            if row["session_last_url"] else None
        ),
    )


def _redacted_payload(action: str) -> str:
    safe: dict[str, Any]
    if action == "open":
        safe = {"url": "https://redacted.invalid"}
    elif action == "click":
        safe = {"selector": "[redacted]"}
    elif action == "type":
        safe = {"selector": "[redacted]", "text": "", "enter": False}
    elif action == "read":
        safe = {"selector": ""}
    elif action == "screenshot":
        safe = {"full_page": False}
    else:
        safe = {}
    return json.dumps(safe, separators=(",", ":"))


__all__ = [
    "MAX_ERROR_CHARS",
    "MAX_RESULT_BYTES",
    "RemoteBrowserJobStateError",
    "SqliteRemoteBrowserJobs",
    "validate_worker_id",
]
