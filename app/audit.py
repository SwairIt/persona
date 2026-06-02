"""Append-only audit log for privileged admin actions (v0.36).

Captures every settings change, bulk-delete, token issuance + revocation
and vault read/write/delete so an operator can review *who did what*
during a security incident. Backed by the ``audit_log`` table created
in migration ``037_audit_log.sql``.

Design contract
---------------
* **Append-only.** Nothing in this module ever ``UPDATE``s or
  ``DELETE``s a row. Retention pruning, if ever needed, must be an
  explicit out-of-band job — never a side-effect of logging.
* **Never log secrets.** Callers MUST pass only key names, ids, counts
  and other non-sensitive metadata. Helpers below accept a free-form
  ``detail`` string; callers are responsible for keeping plaintext out
  of it. The vault routes are the canonical example — they pass the
  *key* (``"openai_api_key"``) but never the *value*.
* **Never let logging break the caller.** :func:`log_action` swallows
  ``sqlite3``/``aiosqlite`` errors and emits a structured warning
  instead. A failed audit insert is a problem to triage, not a reason
  to 500 the user-visible request that triggered it.
* **Parametrised SQL.** All inserts use ``?`` placeholders so audit
  rows themselves are never an injection vector.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Sequence

log = get_logger("persona.audit")

# Cap on how many rows ``list_recent`` will ever return in a single call.
# Pagination through the UI passes ``limit=100`` per page; this ceiling
# keeps a misbehaving caller from materialising the entire table.
_LIST_HARD_CAP = 1000

# Strong references for fire-and-forget tasks scheduled by
# :func:`log_action_sync` — without these the event loop may garbage
# collect the task mid-flight (see asyncio + RUF006 docs).
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()


class AuditRow(TypedDict):
    """Public projection of a row in ``audit_log``."""

    id: int
    ts: str
    action: str
    actor: str | None
    target: str | None
    detail: str | None
    success: bool


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


async def log_action(
    action: str,
    actor: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    success: bool = True,
) -> None:
    """Append a single row to ``audit_log``.

    ``action`` is required (dotted slug like ``"bulk_delete.confirm"``,
    ``"api_token.create"``, ``"vault.set"``). Everything else is
    optional and stored as-is. Empty strings are normalised to ``None``
    so the DB never holds ambiguous blanks.

    Never raises — a failure here logs a structured warning and returns
    silently so the surrounding admin request keeps working.
    """
    cleaned_action = (action or "").strip()
    if not cleaned_action:
        log.warning("audit.log.empty_action")
        return

    cleaned_actor = _empty_to_none(actor)
    cleaned_target = _empty_to_none(target)
    cleaned_detail = _empty_to_none(detail)
    success_flag = 1 if success else 0

    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO audit_log (action, actor, target, detail, success) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    cleaned_action,
                    cleaned_actor,
                    cleaned_target,
                    cleaned_detail,
                    success_flag,
                ),
            )
            await conn.commit()
    except aiosqlite.Error as exc:
        log.warning(
            "audit.log.failed",
            action=cleaned_action,
            target=cleaned_target,
            error=str(exc),
        )
        return

    log.info(
        "audit.log.ok",
        action=cleaned_action,
        actor=cleaned_actor,
        target=cleaned_target,
        success=bool(success_flag),
    )


def log_action_sync(
    action: str,
    actor: str | None = None,
    target: str | None = None,
    detail: str | None = None,
    success: bool = True,
) -> None:
    """Synchronous shorthand for callers stuck outside an async context.

    Schedules :func:`log_action` on the running event loop when one is
    available; otherwise spins up a short-lived loop just to drain the
    coroutine. Either way the audit row reaches SQLite without forcing
    sync callers to refactor into ``async def``.

    Like :func:`log_action`, it never raises.
    """
    coro = log_action(
        action,
        actor=actor,
        target=target,
        detail=detail,
        success=success,
    )
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop on this thread — block briefly to drain the coroutine.
        try:
            asyncio.run(coro)
        except RuntimeError as exc:  # pragma: no cover — defensive
            log.warning("audit.log_sync.no_loop", action=action, error=str(exc))
        return
    # Fire-and-forget: we are already inside an event loop, so scheduling
    # the coroutine is the correct non-blocking path. ``log_action`` swallows
    # its own errors, so we never need to await the task. We keep the task
    # reference on a module-level set so the garbage collector cannot cancel
    # it before it runs (see RUF006 / asyncio docs).
    task = loop.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


async def list_recent(
    limit: int = 200,
    action_like: str | None = None,
    offset: int = 0,
) -> list[AuditRow]:
    """Return audit rows newest first, optionally filtered by action substring.

    ``action_like`` is matched with SQL ``LIKE`` using ``%`` wildcards
    around the user input; the input itself is parameter-bound so no
    SQL injection is possible. ``limit`` is clamped to
    ``_LIST_HARD_CAP`` so a typo (``limit=10**9``) cannot load the whole
    table into memory.
    """
    safe_limit = max(1, min(int(limit), _LIST_HARD_CAP))
    safe_offset = max(0, int(offset))

    # Two fully-static SQL strings, chosen by whether a filter was passed.
    # No interpolation happens at all — values still travel via ``?`` binds.
    params: Sequence[object]
    if action_like and action_like.strip():
        sql = (
            "SELECT id, ts, action, actor, target, detail, success "
            "FROM audit_log WHERE action LIKE ? "
            "ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params = (f"%{action_like.strip()}%", safe_limit, safe_offset)
    else:
        sql = (
            "SELECT id, ts, action, actor, target, detail, success "
            "FROM audit_log "
            "ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params = (safe_limit, safe_offset)
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        log.warning("audit.list.failed", error=str(exc))
        return []

    return [
        AuditRow(
            id=int(row["id"]),
            ts=str(row["ts"]),
            action=str(row["action"]),
            actor=(None if row["actor"] is None else str(row["actor"])),
            target=(None if row["target"] is None else str(row["target"])),
            detail=(None if row["detail"] is None else str(row["detail"])),
            success=bool(int(row["success"])),
        )
        for row in rows
    ]


async def count_recent(action_like: str | None = None) -> int:
    """Return the total number of rows matching ``action_like`` (or all rows).

    Used by the pagination UI to render "Page X of Y" without holding
    every row in memory. Returns ``0`` on any DB error so the page can
    still render rather than 500-ing on a transient SQLite hiccup.
    """
    # Two fully-static SQL strings, same rationale as :func:`list_recent`.
    params: Sequence[object]
    if action_like and action_like.strip():
        sql = "SELECT COUNT(*) AS n FROM audit_log WHERE action LIKE ?"
        params = (f"%{action_like.strip()}%",)
    else:
        sql = "SELECT COUNT(*) AS n FROM audit_log"
        params = ()
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
    except aiosqlite.Error as exc:
        log.warning("audit.count.failed", error=str(exc))
        return 0
    if row is None:
        return 0
    return int(row["n"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_to_none(value: str | None) -> str | None:
    """Normalise ``""`` / whitespace-only strings to ``None``.

    Keeps the DB free of "empty but not NULL" rows that complicate
    filtering and would otherwise have to be handled by every reader.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


__all__ = [
    "AuditRow",
    "count_recent",
    "list_recent",
    "log_action",
    "log_action_sync",
]
