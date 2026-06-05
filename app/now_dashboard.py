"""/now activity dashboard — one-glance "where am I right now" snapshot.

This module assembles a single :class:`NowState` dict from the live
SQLite database. Every read goes through :func:`app.storage.db.get_connection`
and every SQL statement is parametrised; the route layer is a thin
HTTP wrapper around :func:`build_now_state`.

The shape is deliberately a plain ``dict`` (typed via :class:`NowState`)
because both the HTML template and the ``/api/now.json`` endpoint
serialise it straight to the wire — the dict *is* the public contract.

Design notes:

* All "today" aggregates are computed against ``DATE('now')`` in
  SQLite, which uses the SQLite session's local timezone interpreter.
  The captured_at column already stores ISO-8601 UTC, so we compare on
  the ``DATE(captured_at)`` projection — close enough for a dashboard
  that refreshes every 10s.
* Missing optional tables (``ai_reminder`` on a stale install,
  ``audio_segment`` when audio capture has never been enabled) are
  handled by falling back to neutral values rather than raising —
  the page must always render, even on a half-migrated DB.
* :func:`app.focus.current_session` is awaited rather than inlined so
  the focus module remains the single source of truth for the
  "open work block" shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypedDict

import aiosqlite

from app.budget import get_throttle_level
from app.focus import FocusSession, current_session
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.now_dashboard")


# Hard cap on the recent_reminders list. Three is enough to fit the
# dashboard tile without scrolling; older items live on /reminders.
_RECENT_REMINDERS_LIMIT = 3


class LatestShot(TypedDict):
    """One screenshot row, trimmed to what the dashboard needs."""

    id: int
    captured_at: str
    app_name: str | None
    window_title: str | None


class TopApp(TypedDict):
    """Most-screened app today, with its capture count."""

    app_name: str
    count: int


class ActiveMeeting(TypedDict):
    """Last detected meeting + the smart-pause feature flag."""

    enabled: bool
    app_name: str | None
    started_at: str | None


class RecentReminder(TypedDict):
    """One row in the unified recent-reminders feed."""

    kind: str  # "ai" | "todo"
    id: int
    title: str
    severity: str | None  # "info" | "warn" | "action" — None for plain reminders
    due_at: str | None
    created_at: str


class NowState(TypedDict):
    """The complete dashboard payload."""

    latest_shot: LatestShot | None
    active_app: str | None
    last_shot_ago_seconds: int | None
    today_shots_count: int
    today_voice_seconds: int
    today_top_app: TopApp | None
    active_focus: FocusSession | None
    active_meeting: ActiveMeeting
    recent_reminders: list[RecentReminder]
    throttle_level: int
    capture_paused: bool
    audio_paused: bool
    generated_at: str


async def _fetch_latest_shot(conn: aiosqlite.Connection) -> LatestShot | None:
    """Return the most recent screenshot row, trimmed for the dashboard."""
    cursor = await conn.execute(
        "SELECT id, captured_at, app_name, window_title "
        "FROM screenshots "
        "ORDER BY captured_at DESC "
        "LIMIT 1",
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    app_raw = row["app_name"]
    title_raw = row["window_title"]
    return LatestShot(
        id=int(row["id"]),
        captured_at=str(row["captured_at"]),
        app_name=str(app_raw) if app_raw is not None else None,
        window_title=str(title_raw) if title_raw is not None else None,
    )


async def _count_today_shots(conn: aiosqlite.Connection) -> int:
    """Count screenshots whose ``DATE(captured_at) = DATE('now')``."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE DATE(captured_at) = DATE('now')",
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _sum_today_voice_seconds(conn: aiosqlite.Connection) -> int:
    """Sum ``audio_segment.duration_s`` for today (0 on missing table)."""
    try:
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(duration_s), 0) AS total FROM audio_segment "
            "WHERE DATE(started_at) = DATE('now')",
        )
        row = await cursor.fetchone()
    except aiosqlite.OperationalError as exc:
        log.debug("now.audio_segment_missing", error=str(exc))
        return 0
    if row is None:
        return 0
    return int(float(row["total"] or 0.0))


async def _today_top_app(conn: aiosqlite.Connection) -> TopApp | None:
    """Pick the app with the most screenshots today.

    Ignores rows with a NULL ``app_name`` so a missing-window-title
    bug doesn't surface "—" as the top app.
    """
    cursor = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE DATE(captured_at) = DATE('now') AND app_name IS NOT NULL "
        "GROUP BY app_name "
        "ORDER BY n DESC "
        "LIMIT 1",
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return TopApp(app_name=str(row["app_name"]), count=int(row["n"]))


async def _last_meeting(conn: aiosqlite.Connection) -> tuple[str | None, str | None]:
    """Return ``(app_name, started_at)`` for the most recent meeting_event."""
    try:
        cursor = await conn.execute(
            "SELECT app_name, started_at FROM meeting_event "
            "ORDER BY started_at DESC LIMIT 1",
        )
        row = await cursor.fetchone()
    except aiosqlite.OperationalError as exc:
        log.debug("now.meeting_event_missing", error=str(exc))
        return None, None
    if row is None:
        return None, None
    return str(row[0]), str(row[1])


async def _recent_ai_reminders(
    conn: aiosqlite.Connection, limit: int
) -> list[RecentReminder]:
    """Fetch the last ``limit`` undismissed ai_reminder rows."""
    try:
        cursor = await conn.execute(
            "SELECT id, title, severity, due_at, created_at "
            "FROM ai_reminder "
            "WHERE dismissed_at IS NULL "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        log.debug("now.ai_reminder_missing", error=str(exc))
        return []
    result: list[RecentReminder] = []
    for row in rows:
        due_raw = row["due_at"]
        sev_raw = row["severity"]
        result.append(
            RecentReminder(
                kind="ai",
                id=int(row["id"]),
                title=str(row["title"]),
                severity=str(sev_raw) if sev_raw is not None else "info",
                due_at=str(due_raw) if due_raw is not None else None,
                created_at=str(row["created_at"]),
            )
        )
    return result


async def _recent_plain_reminders(
    conn: aiosqlite.Connection, limit: int
) -> list[RecentReminder]:
    """Fetch the last ``limit`` undone reminders (single-day todos).

    "Undismissed" maps to ``done = 0`` for this older table — the spec
    calls it "undismissed" because both surfaces feed the same list.
    """
    try:
        cursor = await conn.execute(
            "SELECT id, body, due_date, created_at "
            "FROM reminders "
            "WHERE done = 0 "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        log.debug("now.reminders_missing", error=str(exc))
        return []
    result: list[RecentReminder] = []
    for row in rows:
        due_raw = row["due_date"]
        result.append(
            RecentReminder(
                kind="todo",
                id=int(row["id"]),
                title=str(row["body"]),
                severity=None,
                due_at=str(due_raw) if due_raw is not None else None,
                created_at=str(row["created_at"]),
            )
        )
    return result


def _merge_reminders(
    ai_items: list[RecentReminder],
    todo_items: list[RecentReminder],
    limit: int,
) -> list[RecentReminder]:
    """Interleave the two sources by ``created_at`` (newest first)."""
    combined = ai_items + todo_items
    combined.sort(key=lambda r: r["created_at"], reverse=True)
    return combined[:limit]


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, treating naive input as UTC."""
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.debug("now.iso_parse_failed", value=text[:40])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _seconds_since(captured_at: str) -> int | None:
    """Return whole seconds between ``captured_at`` and now (UTC)."""
    parsed = _parse_iso(captured_at)
    if parsed is None:
        return None
    delta = datetime.now(UTC) - parsed
    seconds = int(delta.total_seconds())
    # Clamp negatives — a clock skew between the capture process and the
    # web process should never surface as "shot from the future".
    return max(seconds, 0)


def _kv_flag(value: str | None) -> bool:
    """Parse a ``"1"``/``"0"`` kv flag, defaulting to ``False``."""
    return (value or "0").strip() == "1"


async def build_now_state() -> NowState:
    """Assemble the full :class:`NowState` snapshot.

    One connection covers every read so the dashboard is internally
    consistent — a capture landing mid-build can't put us in a state
    where ``latest_shot`` is from 12:01 but ``today_shots_count``
    already reflects the 12:02 row.
    """
    async with get_connection() as conn:
        latest_shot = await _fetch_latest_shot(conn)
        today_shots_count = await _count_today_shots(conn)
        today_voice_seconds = await _sum_today_voice_seconds(conn)
        today_top_app = await _today_top_app(conn)
        meeting_app, meeting_at = await _last_meeting(conn)
        ai_items = await _recent_ai_reminders(conn, _RECENT_REMINDERS_LIMIT)
        todo_items = await _recent_plain_reminders(conn, _RECENT_REMINDERS_LIMIT)
        capture_paused_raw = await get_kv(conn, "capture_screens_disabled")
        audio_paused_raw = await get_kv(conn, "audio_capture_paused_live")
        meeting_enabled_raw = await get_kv(conn, "meeting_pause_enabled")

    # Focus + budget have their own short-lived connections inside their
    # helpers; both are read-only on this path and aiosqlite tolerates
    # multiple concurrent readers in WAL mode, so there's no contention.
    active_focus = await current_session()
    throttle_level = await get_throttle_level()

    active_app: str | None = None
    last_shot_ago_seconds: int | None = None
    if latest_shot is not None:
        active_app = latest_shot["app_name"]
        last_shot_ago_seconds = _seconds_since(latest_shot["captured_at"])

    recent_reminders = _merge_reminders(
        ai_items, todo_items, _RECENT_REMINDERS_LIMIT
    )

    state: NowState = {
        "latest_shot": latest_shot,
        "active_app": active_app,
        "last_shot_ago_seconds": last_shot_ago_seconds,
        "today_shots_count": today_shots_count,
        "today_voice_seconds": today_voice_seconds,
        "today_top_app": today_top_app,
        "active_focus": active_focus,
        "active_meeting": ActiveMeeting(
            enabled=_kv_flag(meeting_enabled_raw),
            app_name=meeting_app,
            started_at=meeting_at,
        ),
        "recent_reminders": recent_reminders,
        "throttle_level": int(throttle_level),
        "capture_paused": _kv_flag(capture_paused_raw),
        "audio_paused": _kv_flag(audio_paused_raw),
        "generated_at": datetime.now(UTC).isoformat(),
    }

    log.info(
        "now.snapshot",
        shots_today=today_shots_count,
        voice_seconds=today_voice_seconds,
        active_app=active_app,
        has_focus=active_focus is not None,
        throttle_level=int(throttle_level),
    )
    return state


def to_jsonable(state: NowState) -> dict[str, Any]:
    """Return a plain ``dict`` suitable for :class:`fastapi.JSONResponse`.

    ``TypedDict`` is already a dict at runtime, but typing it as
    ``dict[str, Any]`` makes the route layer's signature obvious and
    keeps the explicit "this is the wire shape" boundary.
    """
    return dict(state)


__all__ = [
    "ActiveMeeting",
    "LatestShot",
    "NowState",
    "RecentReminder",
    "TopApp",
    "build_now_state",
    "to_jsonable",
]
