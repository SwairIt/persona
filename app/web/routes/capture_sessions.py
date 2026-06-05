"""Capture-sessions diary UI + JSON API (v1.42).

Surfaces :mod:`app.capture_sessions` over HTTP:

* ``GET /sessions`` — table of recent sessions, newest first. Each row
  links to ``/session/{id}`` for a detail breakdown.
* ``GET /session/{id}`` — detail page: linked first / last screenshot,
  the hourly_cards whose hour-window overlaps the session, and any
  ``audio_segment`` transcripts that landed inside the session.
* ``GET /api/sessions.json`` — machine-readable mirror of the list
  view for scripting / external dashboards.

All SQL is parametrised. The HTMLResponse routes pass ``active_nav =
"memory"`` because the sessions log is conceptually a memory artefact
(work-history journal), and the existing nav doesn't have a dedicated
``sessions`` slot.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["capture-sessions"])
log = get_logger("persona.web.capture_sessions")


_LIST_LIMIT = 100
"""How many sessions the list page / JSON endpoint return."""

_HOURLY_CARD_LIMIT = 24
"""Cap on hourly_card rows shown on the detail page (≈ 1 day of cards)."""

_TRANSCRIPT_LIMIT = 50
"""Cap on transcript rows shown on the detail page."""


def _decode_titles(raw: str | None) -> list[str]:
    """Decode ``top_titles_json`` (NULL / malformed → empty list).

    The DB column is nullable and we never want a render to blow up
    just because one ancient row got written with a malformed payload.
    """
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        log.debug("capture_sessions.titles_decode_failed", raw=raw[:120])
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


async def _list_sessions(limit: int) -> list[dict[str, Any]]:
    """Fetch the most recent sessions, newest first, with decoded titles."""
    sql = (
        "SELECT id, started_at, ended_at, duration_seconds, dominant_app, "
        "       screen_count, voice_seconds, top_titles_json, created_at "
        "FROM capture_session "
        "ORDER BY started_at DESC "
        "LIMIT ?"
    )
    async with get_connection() as conn, conn.execute(sql, (limit,)) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "duration_seconds": int(row["duration_seconds"]),
            "dominant_app": row["dominant_app"],
            "screen_count": int(row["screen_count"]),
            "voice_seconds": int(row["voice_seconds"]),
            "top_titles": _decode_titles(row["top_titles_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _fetch_session(session_id: int) -> dict[str, Any] | None:
    """Look up one session row by id (None when not found)."""
    sql = (
        "SELECT id, started_at, ended_at, duration_seconds, dominant_app, "
        "       screen_count, voice_seconds, top_titles_json, created_at "
        "FROM capture_session WHERE id = ?"
    )
    async with get_connection() as conn, conn.execute(sql, (session_id,)) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_seconds": int(row["duration_seconds"]),
        "dominant_app": row["dominant_app"],
        "screen_count": int(row["screen_count"]),
        "voice_seconds": int(row["voice_seconds"]),
        "top_titles": _decode_titles(row["top_titles_json"]),
        "created_at": row["created_at"],
    }


async def _first_last_shots(
    started_at: str,
    ended_at: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Find the first / last screenshot whose ``captured_at`` is in window."""
    sql_first = (
        "SELECT id, captured_at, app_name, window_title "
        "FROM screenshots "
        "WHERE captured_at >= ? AND captured_at <= ? "
        "ORDER BY captured_at ASC LIMIT 1"
    )
    sql_last = (
        "SELECT id, captured_at, app_name, window_title "
        "FROM screenshots "
        "WHERE captured_at >= ? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1"
    )

    async with get_connection() as conn:
        async with conn.execute(sql_first, (started_at, ended_at)) as cursor_first:
            first = await cursor_first.fetchone()
        async with conn.execute(sql_last, (started_at, ended_at)) as cursor_last:
            last = await cursor_last.fetchone()

    def _shape(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "captured_at": row["captured_at"],
            "app_name": row["app_name"],
            "window_title": row["window_title"],
        }

    return _shape(first), _shape(last)


async def _hourly_cards_in_window(
    started_at: str,
    ended_at: str,
) -> list[dict[str, Any]]:
    """Return hourly_card rows whose hour-window overlaps the session.

    Overlap test is the standard ``a.start < b.end AND a.end > b.start``
    form — covers cards that start before / end after the session.
    The table may legitimately be empty (worker disabled), in which
    case we silently return ``[]``.
    """
    sql = (
        "SELECT hour_start, hour_end, summary, screen_count, audio_seconds, "
        "       top_words "
        "FROM hourly_card "
        "WHERE hour_start < ? AND hour_end > ? "
        "ORDER BY hour_start ASC LIMIT ?"
    )
    try:
        async with get_connection() as conn, conn.execute(
            sql, (ended_at, started_at, _HOURLY_CARD_LIMIT)
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception as exc:
        log.debug("capture_sessions.hourly_card_lookup_failed", error=str(exc))
        return []
    return [
        {
            "hour_start": row["hour_start"],
            "hour_end": row["hour_end"],
            "summary": row["summary"],
            "screen_count": int(row["screen_count"]),
            "audio_seconds": int(row["audio_seconds"]),
            "top_words": row["top_words"],
        }
        for row in rows
    ]


async def _transcripts_in_window(
    started_at: str,
    ended_at: str,
) -> list[dict[str, Any]]:
    """Return audio_segment transcripts that fell inside the session.

    Reads the post-migration-093 column names (``captured_at``,
    ``duration_seconds``). Falls back to an empty list if the table is
    absent.
    """
    sql = (
        "SELECT id, captured_at, duration_seconds, transcript, locale "
        "FROM audio_segment "
        "WHERE captured_at >= ? AND captured_at <= ? "
        "  AND transcript IS NOT NULL AND transcript != '' "
        "ORDER BY captured_at ASC LIMIT ?"
    )
    try:
        async with get_connection() as conn, conn.execute(
            sql, (started_at, ended_at, _TRANSCRIPT_LIMIT)
        ) as cursor:
            rows = await cursor.fetchall()
    except Exception as exc:
        log.debug("capture_sessions.transcript_lookup_failed", error=str(exc))
        return []
    return [
        {
            "id": int(row["id"]),
            "captured_at": row["captured_at"],
            "duration_seconds": float(row["duration_seconds"] or 0.0),
            "transcript": row["transcript"],
            "locale": row["locale"],
        }
        for row in rows
    ]


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request) -> HTMLResponse:
    """Render the journal-style sessions list, newest first."""
    sessions = await _list_sessions(limit=_LIST_LIMIT)
    return templates.TemplateResponse(
        request,
        "capture_sessions.html",
        {
            "title": "Сессии работы",
            "active_nav": "memory",
            "sessions": sessions,
        },
    )


@router.get("/session/{session_id}", response_class=HTMLResponse)
async def session_detail_page(
    request: Request, session_id: int
) -> HTMLResponse:
    """Render the per-session breakdown (shots / hourly cards / voice)."""
    session = await _fetch_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    first_shot, last_shot = await _first_last_shots(
        session["started_at"], session["ended_at"]
    )
    hourly_cards = await _hourly_cards_in_window(
        session["started_at"], session["ended_at"]
    )
    transcripts = await _transcripts_in_window(
        session["started_at"], session["ended_at"]
    )

    return templates.TemplateResponse(
        request,
        "capture_session_detail.html",
        {
            "title": f"Сессия #{session_id}",
            "active_nav": "memory",
            "session": session,
            "first_shot": first_shot,
            "last_shot": last_shot,
            "hourly_cards": hourly_cards,
            "transcripts": transcripts,
        },
    )


@router.get("/api/sessions.json")
async def sessions_json() -> JSONResponse:
    """Return the most recent sessions as JSON for scripting / dashboards."""
    sessions = await _list_sessions(limit=_LIST_LIMIT)
    return JSONResponse({"sessions": sessions})


__all__ = ["router"]
