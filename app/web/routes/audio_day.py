"""Per-day audio timeline — speech segments grouped by hour with playback.

v1.11 feature 3/3, route 1 of 5. Renders one calendar day's worth of
rows from the ``audio_segment`` table (created in v1.11 feature 1/3)
as a vertical, hour-grouped timeline. Each row surfaces:

* the local-time ``HH:MM:SS`` the segment started
* its duration in seconds
* the codec + bitrate (``opus @ 24 kbps``)
* the on-disk size (humanised — KB / MB)
* the transcript inline (if any — the Whisper worker may not have
  reached this row yet, in which case we render a discreet "transcript
  pending" hint instead of an empty paragraph)
* an HTML5 ``<audio controls>`` pointing at
  ``/audio/segment/{id}`` — but **only** when ``path`` is still
  populated; the hot-tier retention purge clears the column once the
  audio bytes are reaped, leaving a transcript-only row behind. That
  text-only state is the steady-state for old days, so it has to look
  intentional rather than like a broken player.

Shape contract
--------------

Row dict produced by :func:`_project_row`:

``{
    "id": int,
    "captured_at": str (ISO-8601 UTC),
    "duration_seconds": float,
    "codec": str,
    "bitrate": int,
    "size_bytes": int,
    "transcript": str,
    "has_audio": bool,
}``

``has_audio`` is the *post*-retention flag the template gates the
``<audio>`` tag on. Rows where ``path IS NULL OR path = ''`` get
``False`` so the player never appears with a dead ``src``.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main` — the task spec forbids touching
``main.py``. Wire it up in a follow-up patch with::

    from app.web.routes import audio_day as audio_day_routes
    app.include_router(audio_day_routes.router)
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.audio.web")

router = APIRouter(tags=["audio-day"])

# Hard cap on segments rendered for a single day. A continuously-recorded
# 24h day chopped into 30-second clips fits easily under this; ten
# thousand rows would lock up the browser on input. Matches the same
# defence the day-scrubber applies on the screenshot side.
_MAX_SEGMENTS_PER_DAY: Final[int] = 5_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_local() -> date:
    """Local-date "today" — matches the wall clock the operator sees."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to local today on any failure.

    Matches the day-scrubber / notes-timeline convention: a malformed
    path component silently lands on today's view rather than 400-ing.
    A timeline is exploratory — surfacing *something* useful beats a
    stack trace.
    """
    if day is None or day == "":
        return _today_local()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        log.info("audio.day.invalid_fallback_today", value=day)
        return _today_local()


def _project_row(row: Any) -> dict[str, Any]:
    """Project an :class:`aiosqlite.Row` into the timeline contract dict.

    Centralises the SQL-row → template-context coercion so the
    Jinja side only ever sees a single, well-typed shape. The on-disk
    ``path`` is *never* surfaced (matches :mod:`note_attachments` —
    clients fetch bytes through the streaming endpoint, not the raw
    filesystem path); we only emit the boolean ``has_audio`` flag the
    template uses to decide whether to render the ``<audio>`` element.
    """
    stored_path = row["path"]
    has_audio = bool(stored_path is not None and str(stored_path).strip() != "")
    transcript_raw = row["transcript"]
    transcript = "" if transcript_raw is None else str(transcript_raw)
    return {
        "id": int(row["id"]),
        "captured_at": str(row["captured_at"]),
        "duration_seconds": float(row["duration_seconds"] or 0.0),
        "codec": str(row["codec"] or ""),
        "bitrate": int(row["bitrate"] or 0),
        "size_bytes": int(row["size_bytes"] or 0),
        "transcript": transcript,
        "has_audio": has_audio,
    }


async def _load_day_segments(day_value: date) -> list[dict[str, Any]]:
    """Fetch every audio segment whose ``date(captured_at) = day_value``.

    Uses SQLite's ``date(...)`` function on the stored ISO timestamp so
    the filter matches the same wall-clock day grouping the rest of
    the app applies (segments are written via ``datetime('now')`` —
    SQLite emits UTC there, the grouping is internally consistent).

    The cap on returned rows is enforced server-side and surfaces in
    the template as a ``truncated`` flag when hit.
    """
    day_str = day_value.strftime("%Y-%m-%d")
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id,
                   captured_at,
                   duration_seconds,
                   codec,
                   bitrate,
                   size_bytes,
                   path,
                   transcript
              FROM audio_segment
             WHERE date(captured_at) = ?
             ORDER BY captured_at ASC, id ASC
             LIMIT ?
            """,
            (day_str, _MAX_SEGMENTS_PER_DAY),
        )
        rows = await cursor.fetchall()

    return [_project_row(row) for row in rows]


def _group_by_hour(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket projected rows by their UTC-hour prefix in encounter order.

    The grouping key is the leading ``YYYY-MM-DDTHH`` slice of
    ``captured_at`` — operating on the ISO string keeps the helper
    pure-Python (no ``datetime.fromisoformat`` per row, which adds up
    quickly on a 5 000-row day) and matches the ordering already
    imposed by the SQL ``ORDER BY captured_at`` so an ``OrderedDict``
    preserves chronological hour blocks.

    Each output bucket is a dict ``{"hour": "HH:00", "items": [...]}``;
    the bare hour label is what the template stamps as the section
    heading. We deliberately leave the date out of the heading — the
    page already carries the day in its ``<h1>``.
    """
    buckets: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for item in items:
        captured = str(item.get("captured_at") or "")
        # ``YYYY-MM-DDTHH`` is 13 chars; anything shorter is malformed
        # input and falls into a single "unknown" bucket rather than
        # crashing the page.
        if len(captured) >= 13 and captured[10] in {"T", " "}:
            hour_key = captured[:13]
        else:
            hour_key = "unknown"
        buckets.setdefault(hour_key, []).append(item)
    return [
        {"hour": _hour_label_for(hour_key, rows), "items": rows}
        for hour_key, rows in buckets.items()
    ]


def _hour_label_for(hour_key: str, rows: list[dict[str, Any]]) -> str:
    """Render the section heading for one hour bucket.

    Pulls the ``HH:00`` slice off the first row's ``captured_at`` so a
    locale-specific renderer downstream can swap it for a 12-hour clock
    without forcing us to reach into Jinja. Falls back to ``"—"`` for
    the synthetic ``"unknown"`` bucket so the heading is never empty.
    """
    if hour_key == "unknown" or not rows:
        return "—"
    captured = str(rows[0].get("captured_at") or "")
    if len(captured) >= 13 and captured[10] in {"T", " "}:
        return captured[11:13] + ":00"
    return "—"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/audio/day/{day_iso}", response_class=HTMLResponse)
async def audio_day_page(request: Request, day_iso: str) -> HTMLResponse:
    """Render the per-day audio segment timeline as HTML.

    Each row receives ``id="audio-{id}"`` so callers can deep-link to a
    specific segment by appending ``#audio-42`` to the URL. The
    ``<audio>`` element is only emitted when the row still has a
    non-empty ``path`` — after the hot-tier retention purge the row
    survives transcript-only and the template renders the text alone.
    """
    day_value = _parse_day_or_today(day_iso)
    items = await _load_day_segments(day_value)
    hour_groups = _group_by_hour(items)

    total_duration = sum(float(item["duration_seconds"]) for item in items)
    total_bytes = sum(int(item["size_bytes"]) for item in items)
    with_audio = sum(1 for item in items if item["has_audio"])

    log.info(
        "audio.day.page",
        day=day_value.isoformat(),
        count=len(items),
        with_audio=with_audio,
        hours=len(hour_groups),
        total_duration_seconds=total_duration,
    )

    return templates.TemplateResponse(
        request,
        "audio_day.html",
        {
            "title": f"Audio — {day_value.isoformat()}",
            "active_nav": "timeline",
            "day": day_value.isoformat(),
            # The context key is ``segments`` rather than ``items`` because
            # :file:`base.html` does ``{% set items = [...] %}`` for its nav
            # — extending it would otherwise shadow our list with the nav
            # tuples, silently rendering an empty timeline.
            "segments": items,
            "hour_groups": hour_groups,
            "total": len(items),
            "with_audio": with_audio,
            "total_duration_seconds": total_duration,
            "total_bytes": total_bytes,
            "truncated": len(items) >= _MAX_SEGMENTS_PER_DAY,
        },
    )


__all__ = ["router"]
