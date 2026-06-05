"""Focus-aware in-browser notification queue (v1.44).

A small structured event log the rest of the codebase can push into so
the UI's bell widget (and the SSE channel that feeds it — see
:mod:`app.web.routes.notifications`) can surface things the operator
should look at: capture stopped, OCR backlog spike, a long-read clipped,
a privacy bundle silently disabled, etc.

Design notes
------------
* The store is the dedicated ``notification`` table introduced by
  migration ``118_notifications.sql``. Producers call :func:`push`,
  consumers call :func:`list_unseen` / :func:`mark_seen` /
  :func:`mark_all_seen`.
* ``severity`` is a three-valued tag (``info`` / ``warn`` / ``error``)
  enforced by a CHECK constraint on the table — invalid producer input
  fails fast at write time rather than silently mis-rendering as the
  default tone.
* Every helper opens its own connection via
  :func:`app.storage.db.get_connection` for consistency with the rest
  of the codebase. The table is small (typical lifetime <= a few
  hundred rows for an active operator) so short-lived connections
  don't matter.
* ``structlog`` logs (``persona.notifications``) never include the
  free-form ``body`` — only the stable ``kind``, ``severity`` and the
  generated row id.
"""

from __future__ import annotations

from typing import Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.notifications")

_ALLOWED_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "warn", "error"})
"""Mirror of the SQL CHECK constraint. We pre-validate so the error
surfaces as a tidy :class:`ValueError` at the call site instead of an
``aiosqlite.IntegrityError`` deep inside the executor."""


async def push(
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    severity: str = "info",
) -> int:
    """Append a notification row and return the new row id.

    Args:
        kind: Short machine-friendly category (``"capture.stopped"``,
            ``"ocr.backlog"``, ``"longread.clipped"``, ...). Used by the
            UI to pick an icon and by metrics queries to group events.
        title: Single-line human headline shown in the bell list.
        body: Optional longer message. ``None`` means the bell row
            collapses to just the title.
        link: Optional URL the bell row links to (e.g. the dashboard
            tile that explains the event).
        severity: One of ``"info"``, ``"warn"``, ``"error"``.

    Raises:
        ValueError: If ``severity`` is not in :data:`_ALLOWED_SEVERITIES`.
            We surface this before the INSERT so the producer gets a
            stack at the call site rather than an opaque DB error.
    """
    if severity not in _ALLOWED_SEVERITIES:
        msg = f"severity must be one of {sorted(_ALLOWED_SEVERITIES)!r}, got {severity!r}"
        raise ValueError(msg)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO notification (kind, title, body, link, severity) VALUES (?, ?, ?, ?, ?)",
            (kind, title, body, link, severity),
        )
        await conn.commit()
        last_id = cursor.lastrowid

    if last_id is None:
        # aiosqlite is expected to populate lastrowid on INSERT. A None
        # here means the driver returned an unexpected state — bail
        # loudly so the producer notices instead of pretending success.
        msg = "notification insert returned no lastrowid"
        raise RuntimeError(msg)

    notif_id = int(last_id)
    log.info(
        "notifications.push",
        notif_id=notif_id,
        kind=kind,
        severity=severity,
        has_body=body is not None,
        has_link=link is not None,
    )
    return notif_id


async def list_unseen(limit: int = 50) -> list[dict[str, object]]:
    """Return up to ``limit`` unseen notifications, newest first.

    The SSE poll loop and the bell-list endpoint both call this. We
    cap at ``limit`` (default 50) so a pathological producer that
    accidentally pushes thousands of rows doesn't blow up the response
    payload — the operator clicks "mark all read" and the firehose
    quiets down.
    """
    safe_limit = max(0, int(limit))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, kind, title, body, link, severity, created_at "
            "FROM notification "
            "WHERE seen_at IS NULL "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?",
            (safe_limit,),
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row["id"]),
            "kind": str(row["kind"]),
            "title": str(row["title"]),
            "body": (None if row["body"] is None else str(row["body"])),
            "link": (None if row["link"] is None else str(row["link"])),
            "severity": str(row["severity"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def mark_seen(notif_id: int) -> None:
    """Stamp ``seen_at`` on a single notification row.

    No-op (silently) when ``notif_id`` does not exist or is already
    seen — the bell UI does optimistic updates and we don't want a
    double-click to surface a 404.
    """
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE notification SET seen_at = datetime('now') WHERE id = ? AND seen_at IS NULL",
            (int(notif_id),),
        )
        await conn.commit()
    log.info("notifications.mark_seen", notif_id=int(notif_id))


async def mark_all_seen() -> int:
    """Stamp every unseen notification and return how many rows changed.

    The bell's "clear badge" button hits the corresponding route which
    calls this. We return the row count so the route can include it
    in the structured log line — useful when diagnosing "I cleared 0
    rows and the badge stuck" reports.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE notification SET seen_at = datetime('now') WHERE seen_at IS NULL"
        )
        await conn.commit()
        changed = cursor.rowcount or 0
    cleared = int(changed)
    log.info("notifications.mark_all_seen", cleared=cleared)
    return cleared


__all__ = [
    "list_unseen",
    "mark_all_seen",
    "mark_seen",
    "push",
]
