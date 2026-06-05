"""CSV export routes.

Two distinct surfaces share this router for historical reasons:

1. ``/api/export/search.csv`` and ``/api/export/search.md`` — the legacy
   search-results exporter, kept verbatim so the existing front-end
   bookmark links never 404. These ``fetchall()`` against a capped
   ``limit=10000`` and stream-back the whole buffer as a single
   ``Response``; fine for the "top hits" use-case they target.

2. ``/export/screenshots.csv``, ``/export/notes.csv``,
   ``/export/hourly-cards.csv``, ``/export/audio-segments.csv`` plus the
   ``/export/bulk`` landing page — the bulk dump exporter implemented
   on top of :mod:`app.csv_export`. These use id-seek pagination and
   stream row-by-row so a 1 M-row ``screenshots`` table dumps with
   bounded memory and never pins the SQLite connection.

The split is intentional — the legacy paths were already wired into
``main.py`` (``app.include_router(csv_export.router)``) and the task
forbids touching that file, so the new bulk routes ride along on the
same router rather than being registered separately.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from app.csv_export import (
    stream_audio_segments_csv,
    stream_hourly_cards_csv,
    stream_notes_csv,
    stream_screenshots_csv,
)
from app.logging_setup import get_logger
from app.search import search
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.csv_export.routes")

router = APIRouter(tags=["export"])

# Shared media type for every bulk dump. ``charset=utf-8`` is essential —
# Excel on Windows otherwise opens the file with the system locale and
# mangles every non-ASCII OCR character.
_CSV_MEDIA_TYPE = "text/csv; charset=utf-8"


def _filename(stem: str) -> str:
    """Build a dated ``Content-Disposition`` filename for a bulk export."""
    return f"persona-{stem}-{date.today().isoformat()}.csv"


def _stream_headers(stem: str) -> dict[str, str]:
    """Standard headers for every bulk-dump ``StreamingResponse``.

    ``Cache-Control: no-store`` — these dumps are user-specific and
    expensive; never want a proxy or browser to serve a stale copy.
    """
    return {
        "Content-Disposition": f'attachment; filename="{_filename(stem)}"',
        "Cache-Control": "no-store",
    }


async def _to_bytes(source: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """Encode each yielded CSV chunk as UTF-8 for the HTTP body.

    The generators in :mod:`app.csv_export` yield ``str`` — easier to
    unit-test against the RFC 4180 escape contract — but
    :class:`StreamingResponse` expects bytes when the media type is set.
    Encoding in this thin adapter keeps the export module pure-Python
    and the route module HTTP-aware.
    """
    async for chunk in source:
        yield chunk.encode("utf-8")


# ---------------------------------------------------------------------------
# Bulk dump endpoints — paginated, streaming, memory-bounded.
# ---------------------------------------------------------------------------


@router.get("/export/screenshots.csv", response_model=None)
async def export_screenshots_csv(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream the ``screenshots`` table as paginated CSV."""
    log.info("bulk.screenshots.start", date_from=date_from, date_to=date_to)
    return StreamingResponse(
        _to_bytes(stream_screenshots_csv(date_from, date_to)),
        media_type=_CSV_MEDIA_TYPE,
        headers=_stream_headers("screenshots"),
    )


@router.get("/export/notes.csv", response_model=None)
async def export_notes_csv(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream the ``screenshot_notes`` table as paginated CSV."""
    log.info("bulk.notes.start", date_from=date_from, date_to=date_to)
    return StreamingResponse(
        _to_bytes(stream_notes_csv(date_from, date_to)),
        media_type=_CSV_MEDIA_TYPE,
        headers=_stream_headers("notes"),
    )


@router.get("/export/hourly-cards.csv", response_model=None)
async def export_hourly_cards_csv(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream the ``hourly_card`` table as paginated CSV."""
    log.info("bulk.hourly_cards.start", date_from=date_from, date_to=date_to)
    return StreamingResponse(
        _to_bytes(stream_hourly_cards_csv(date_from, date_to)),
        media_type=_CSV_MEDIA_TYPE,
        headers=_stream_headers("hourly-cards"),
    )


@router.get("/export/audio-segments.csv", response_model=None)
async def export_audio_segments_csv(
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream the ``audio_segment`` table as paginated CSV."""
    log.info("bulk.audio_segments.start", date_from=date_from, date_to=date_to)
    return StreamingResponse(
        _to_bytes(stream_audio_segments_csv(date_from, date_to)),
        media_type=_CSV_MEDIA_TYPE,
        headers=_stream_headers("audio-segments"),
    )


@router.get("/export/bulk", response_class=HTMLResponse)
async def export_bulk_landing(request: Request) -> HTMLResponse:
    """Landing page with date pickers + the four download links."""
    log.info("bulk.landing.render")
    return templates.TemplateResponse(
        request,
        "csv_export.html",
        {
            "title": "Bulk CSV export",
            "active_nav": "settings",
        },
    )


# ---------------------------------------------------------------------------
# Legacy search-result exporter — preserved verbatim so existing
# ``/api/export/search.csv`` and ``/api/export/search.md`` bookmarks
# continue to work after the bulk dump rewrite.
# ---------------------------------------------------------------------------


@router.get("/api/export/search.csv")
async def export_search_csv(
    q: str = Query(default=""),
    app_name: str | None = Query(default=None, alias="app"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> Response:
    """Render a search-results page as a single CSV download (legacy)."""
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    async with get_connection() as conn:
        hits = await search(
            conn,
            query=q,
            limit=10000,
            since=since_dt,
            until=until_dt,
            app_name=app_name,
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["captured_at", "app_name", "window_title", "snippet", "screenshot_id", "rank"],
    )
    for h in hits:
        writer.writerow(
            [
                h.captured_at.isoformat(),
                h.app_name or "",
                h.window_title or "",
                _strip_marks(h.snippet),
                h.screenshot_id,
                round(h.rank, 4),
            ],
        )

    filename = f"persona-search-{q or 'all'}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type=_CSV_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _strip_marks(snippet: str) -> str:
    """Drop the ``<mark>`` highlight tags FTS5 wraps around match terms."""
    return snippet.replace("<mark>", "").replace("</mark>", "")


@router.get("/api/export/search.md")
async def export_search_markdown(
    q: str = Query(default=""),
    app_name: str | None = Query(default=None, alias="app"),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> Response:
    """Render search results as Markdown — handy for pasting into a journal."""
    since_dt = datetime.fromisoformat(since) if since else None
    until_dt = datetime.fromisoformat(until) if until else None

    async with get_connection() as conn:
        hits = await search(
            conn,
            query=q,
            limit=500,
            since=since_dt,
            until=until_dt,
            app_name=app_name,
        )

    lines: list[str] = [f"# Persona — search: `{q or 'all recent'}`", ""]
    if hits:
        for h in hits:
            ts = h.captured_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"## {ts} — {h.app_name or '—'}")
            if h.window_title:
                lines.append(f"_{h.window_title}_")
            lines.append("")
            if h.snippet:
                lines.append("> " + _strip_marks(h.snippet))
            lines.append(f"[screenshot #{h.screenshot_id}](/screenshot/{h.screenshot_id})")
            lines.append("")
    else:
        lines.append("No matches.")

    body = "\n".join(lines)
    filename = f"persona-search-{q or 'all'}.md"
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["router"]
