"""Browser push-notification opt-in (v0.66).

A deliberately minimal, server-driven notification queue: nothing is
actually "pushed" by the server — the page polls
:func:`/api/push-notif/pending` every five minutes and the client-side
:file:`push_notif.js` materialises each row as a ``new Notification(...)``.
The opt-in state lives in ``kv_settings`` under
``push_notif_enabled`` (``"1"`` when on, absent / non-``"1"`` when off),
and a sibling row ``push_notif_last_poll`` tracks the high-water-mark
timestamp of the last poll so successive calls only ever surface freshly
queued items.

The "queue" itself is the existing ``reminders`` table — anything
created since the last poll and not yet done is fair game for a
notification. Using an existing source keeps the surface area tiny and
avoids inventing a parallel store that would inevitably drift; the
trade-off is that disabling notifications and re-enabling them will
"replay" reminders created during the off window, which matches the
implicit user expectation of "tell me what I missed".

The Notification permission prompt is intentionally *not* triggered by
this module — browsers reject permission requests that aren't tied to a
user gesture, so the click handler in :file:`push_notif.js` owns that
half of the dance and only POSTs ``/enable`` once permission is granted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

if TYPE_CHECKING:
    import aiosqlite

router = APIRouter(tags=["push-notif"])
log = get_logger("persona.push_notif")

_KV_ENABLED: Final[str] = "push_notif_enabled"
_KV_LAST_POLL: Final[str] = "push_notif_last_poll"
_ENABLED_VALUE: Final[str] = "1"


def _now_iso() -> str:
    """Return a UTC ISO-8601 timestamp with second precision.

    SQLite's ``datetime('now')`` emits ``YYYY-MM-DD HH:MM:SS`` (no
    timezone, implicitly UTC); we mirror that shape so string-comparison
    against ``reminders.created_at`` is well-defined without parsing.
    """
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def _fetch_pending(
    conn: aiosqlite.Connection,
    since: str | None,
) -> list[dict[str, str]]:
    """Return reminder rows queued after ``since`` as notification dicts.

    A ``None`` ``since`` means "no previous poll on record" — we return
    an empty list rather than dumping every historical reminder as a
    notification storm on the first call.
    """
    if since is None:
        return []
    cursor = await conn.execute(
        "SELECT body, due_date FROM reminders "
        "WHERE done = 0 AND created_at > ? "
        "ORDER BY created_at ASC",
        (since,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "title": "Reminder",
            "body": f"{row['body']} (due {row['due_date']})",
        }
        for row in rows
    ]


@router.post("/api/push-notif/enable")
async def push_notif_enable() -> JSONResponse:
    """Mark notifications as enabled and seed the last-poll watermark.

    Seeding ``push_notif_last_poll`` at enable-time means the very first
    ``/pending`` call after opt-in returns only reminders created
    *after* the user opted in, not the entire pre-existing backlog.
    """
    now = _now_iso()
    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, _ENABLED_VALUE)
        await set_kv(conn, _KV_LAST_POLL, now)
    log.info("push_notif.enabled", since=now)
    return JSONResponse({"enabled": True, "since": now})


@router.post("/api/push-notif/disable")
async def push_notif_disable() -> JSONResponse:
    """Clear the opt-in flag; the watermark is left intact on purpose.

    Keeping ``push_notif_last_poll`` lets a subsequent re-enable resume
    from the previous high-water-mark rather than silently dropping any
    reminders created during the off window — symmetrical with the
    "replay what I missed" behaviour described in the module docstring.
    """
    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, "")
    log.info("push_notif.disabled")
    return JSONResponse({"enabled": False})


@router.get("/api/push-notif/pending")
async def push_notif_pending() -> JSONResponse:
    """Return reminders queued since the last poll; advance the watermark.

    The watermark is advanced *after* the read so a crash mid-render
    can't strand notifications in a gap between the SELECT and the
    UPDATE. If notifications are disabled we return an empty list
    without touching the watermark — that way an accidental poll from a
    stale tab can't "consume" reminders the user never saw.
    """
    async with get_connection() as conn:
        enabled = await get_kv(conn, _KV_ENABLED)
        if enabled != _ENABLED_VALUE:
            return JSONResponse({"notifications": []})

        since = await get_kv(conn, _KV_LAST_POLL)
        notifications = await _fetch_pending(conn, since)
        now = _now_iso()
        await set_kv(conn, _KV_LAST_POLL, now)

    log.info("push_notif.pending", count=len(notifications), since=since, now=now)
    return JSONResponse({"notifications": notifications})
