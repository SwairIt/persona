"""Per-day notes timeline — chronological vertical view of one day's notes.

v0.47 feature 1/3. Adds two endpoints, both keyed off the local calendar
day stored in the standalone ``notes`` table (created in
``039_inbox_notes.sql`` and extended with ``encrypted`` / ``ciphertext``
in ``045_encrypted_notes.sql``):

    * ``GET /notes/day/{day}``           — renders ``notes_day.html``.
      Every note row gets an ``id="note-{id}"`` anchor so callers can
      deep-link straight to a single entry inside the timeline.
    * ``GET /api/notes/day/{day}.json``  — machine-readable equivalent
      for the bookmarklet, command palette and any future automation.
      The shape mirrors the public list-endpoint contract in
      :mod:`app.web.routes.notes`: encrypted rows come back with an
      empty ``body`` and the ``[locked]`` marker; the ciphertext blob
      is **never** serialised.

The ``day`` path component is ``YYYY-MM-DD``. A malformed value falls
back to *today* — same forgiving behaviour as the day-scrubber and
day-kanban routes; punishing typos on an exploratory view is
user-hostile.

Markdown rendering on the HTML page is a soft dependency: we try to
import ``markdown`` (PyPI ``markdown`` package — pure stdlib otherwise,
ships no native extensions) and pass each unlocked body through it.
When the import fails the template renders the body inside an escaped
``<pre>`` block instead, so a fresh checkout that hasn't synced the
optional dep still produces a readable timeline. The flag is computed
once at import time, not per request.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``. Wire
it up in a follow-up patch with::

    from app.web.routes import notes_timeline as notes_timeline_routes
    app.include_router(notes_timeline_routes.router)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.notes.timeline")

# Optional Markdown rendering. The package isn't a hard dep — when it's
# missing the template falls back to an escaped ``<pre>`` block. Probed
# once at import time so the per-request hot path is a single bool read.
try:
    import markdown as _markdown_mod

    _markdown: Any | None = _markdown_mod
except ImportError:  # pragma: no cover — exercised only when the dep is absent
    _markdown = None

_MARKDOWN_AVAILABLE: Final[bool] = _markdown is not None

# Same sentinel as :mod:`app.web.routes.notes` so HTML, JSON, CLI and
# TUI clients all surface the *exact* same literal for locked rows.
LOCKED_MARKER: Final[str] = "[locked]"

# Hard cap on notes returned for a single day. A power user dropping a
# hundred snippets into the inbox is normal; ten thousand isn't, and
# rendering that many DOM nodes makes the timeline page unusable.
_MAX_NOTES_PER_DAY: Final[int] = 1_000

router = APIRouter(tags=["notes-timeline"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_local() -> date:
    """Local-date "today" — matches what the wall clock + other day-views show."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to local today on any failure.

    Matches the day-scrubber / day-kanban convention: a bad path lands
    on today rather than 400-ing. A timeline is exploratory — surfacing
    *something* useful beats a stack trace.
    """
    if day is None or day == "":
        return _today_local()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        log.info("notes.timeline.day_invalid_fallback_today", value=day)
        return _today_local()


def _render_body(body: str) -> str:
    """Convert a plaintext markdown body into safe-for-template HTML.

    When the optional ``markdown`` dep is available we render it (with
    the stdlib-friendly ``fenced_code`` and ``tables`` extensions, both
    pure-Python and bundled with the package — no native compilation).
    Otherwise we surface a flag that tells the template to wrap the raw
    string in an escaped ``<pre>``.

    The return value is **always** a string; the template gates on
    ``markdown_available`` to decide whether to ``| safe`` it or escape
    it. Centralising the conversion here keeps the per-row Jinja loop
    free of conditional logic.
    """
    if _MARKDOWN_AVAILABLE and _markdown is not None:
        # ``fenced_code`` + ``tables`` are the two extensions shipped
        # in-tree with the package; both are pure Python.
        return str(
            _markdown.markdown(
                body,
                extensions=["fenced_code", "tables"],
                output_format="html5",
            )
        )
    # Fallback: hand the raw body back; the template will escape it.
    return body


def _project_row(row: Any) -> dict[str, Any]:
    """Build a single timeline row dict (used by both HTML + JSON).

    Encrypted rows replace ``body`` with the empty string and add a
    ``marker`` field carrying the literal ``[locked]`` sentinel; this
    matches the contract used by :mod:`app.web.routes.notes` so callers
    that already handle the list endpoint don't need a separate code
    path for the per-day variant.

    ``tags`` is materialised by the SQL ``group_concat`` and split into
    a real list here so the JSON shape is ``["a", "b"]`` rather than
    ``"a,b"``. An empty tag set comes through as ``[]``.
    """
    is_encrypted = bool(int(row["encrypted"] or 0))
    raw_tags = row["tags"]
    if raw_tags is None or str(raw_tags) == "":
        tags: list[str] = []
    else:
        tags = [t for t in str(raw_tags).split(",") if t]

    item: dict[str, Any] = {
        "id": int(row["id"]),
        "title": (str(row["title"]) if row["title"] is not None else None),
        "created_at": str(row["created_at"]),
        "tags": tags,
        "encrypted": is_encrypted,
    }
    if is_encrypted:
        item["body"] = ""
        item["marker"] = LOCKED_MARKER
    else:
        item["body"] = str(row["body"])
    return item


async def _load_day_notes(day_value: date) -> list[dict[str, Any]]:
    """Fetch every note whose ``date(created_at) = day_value``.

    Uses SQLite's ``date(...)`` function on the stored ISO timestamp so
    the filter matches the same wall-clock day the notes table itself
    was written against (rows are inserted via ``datetime('now')`` —
    SQLite emits UTC there, but day-grouping by the same ``date(...)``
    keeps the query self-consistent regardless of the user's tz).

    Tags are joined in a left-outer ``group_concat`` so the projection
    is one row per note. The cap on returned rows is enforced server-
    side and surfaces in the JSON response as ``truncated`` when hit.
    """
    day_str = day_value.strftime("%Y-%m-%d")
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT n.id,
                   n.title,
                   n.body,
                   n.created_at,
                   n.encrypted,
                   (
                     SELECT group_concat(t.name, ',')
                       FROM note_tags nt
                       JOIN tags t ON t.id = nt.tag_id
                      WHERE nt.note_id = n.id
                   ) AS tags
              FROM notes n
             WHERE date(n.created_at) = ?
             ORDER BY n.created_at ASC, n.id ASC
             LIMIT ?
            """,
            (day_str, _MAX_NOTES_PER_DAY),
        )
        rows = await cursor.fetchall()

    return [_project_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/notes/day/{day}", response_class=HTMLResponse)
async def notes_day_page(request: Request, day: str) -> HTMLResponse:
    """Render the per-day notes timeline as HTML.

    Each note row receives ``id="note-{id}"`` so callers can deep-link
    to a specific entry by appending ``#note-42`` to the URL.
    """
    day_value = _parse_day_or_today(day)
    items = await _load_day_notes(day_value)

    # Pre-render bodies for non-encrypted rows. Encrypted rows keep the
    # empty-string body and let the template show the unlock link.
    for item in items:
        if not item["encrypted"]:
            item["rendered_body"] = _render_body(str(item["body"]))
        else:
            item["rendered_body"] = ""

    log.info(
        "notes.timeline.page",
        day=day_value.isoformat(),
        count=len(items),
        markdown=_MARKDOWN_AVAILABLE,
    )

    return templates.TemplateResponse(
        request,
        "notes_day.html",
        {
            "title": f"Notes — {day_value.isoformat()}",
            "active_nav": "journal",
            "day": day_value.isoformat(),
            # The context key is ``notes`` rather than ``items`` because
            # ``base.html`` does ``{% set items = [...] %}`` for its nav
            # — extending it would otherwise shadow our list with the
            # nav tuples, silently rendering an empty timeline.
            "notes": items,
            "total": len(items),
            "truncated": len(items) >= _MAX_NOTES_PER_DAY,
            "markdown_available": _MARKDOWN_AVAILABLE,
            "locked_marker": LOCKED_MARKER,
        },
    )


@router.get("/api/notes/day/{day}.json", response_class=JSONResponse)
async def notes_day_json(day: str) -> JSONResponse:
    """Machine-readable companion to :func:`notes_day_page`.

    Shape per item: ``{id, title, created_at, body, tags, encrypted}``;
    encrypted items additionally carry ``marker == "[locked]"`` and a
    blanked ``body``. Plaintext bodies are returned raw — no Markdown
    rendering on the JSON side, since downstream automation typically
    wants the source string.
    """
    day_value = _parse_day_or_today(day)
    items = await _load_day_notes(day_value)
    log.info("notes.timeline.json", day=day_value.isoformat(), count=len(items))
    return JSONResponse(
        {
            "day": day_value.isoformat(),
            "total": len(items),
            "truncated": len(items) >= _MAX_NOTES_PER_DAY,
            "items": items,
        }
    )


__all__ = ["LOCKED_MARKER", "router"]
