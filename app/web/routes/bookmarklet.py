"""Browser bookmarklet — drag-to-bookmarks-bar capture into a Persona note.

The bookmarklet runs on whatever site the user is currently viewing, so the
POST endpoint here is the only Persona route that must answer cross-origin.
We keep CORS narrow: ``Access-Control-Allow-Origin: *`` is set only on the
two bookmarklet routes (POST + OPTIONS preflight), the rest of the app keeps
its same-origin defaults from :mod:`app.web.main`.

Auth note: the global ``ApiAuthMiddleware`` lets unauthenticated cookies/host
sessions through for browser navigation, same as every other UI route — the
bookmarklet posts with ``credentials: 'include'`` so the user's existing
Persona session cookie travels along automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.notes import insert_inbox_note
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Mapping

log = get_logger("persona.bookmarklet")

router = APIRouter(tags=["bookmarklet"])

# Hard cap on the user's selected text. Larger selections are silently
# truncated rather than 400'd — a 6000-char selection is still a valid
# capture, we just don't want the note body to grow without bound.
_MAX_SELECTION_CHARS: Final[int] = 5000
# Defensive caps on the remaining free-text fields so a malicious page
# can't push megabytes into the notes table.
_MAX_URL_CHARS: Final[int] = 4096
_MAX_TITLE_CHARS: Final[int] = 1024
_MAX_HTML_CHARS: Final[int] = 50_000

_BOOKMARKLET_JS_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent / "static" / "bookmarklet_source.js"
)

_CORS_HEADERS: Final[Mapping[str, str]] = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}


class CapturePayload(BaseModel):
    """JSON body posted by the bookmarklet."""

    url: str = Field(..., max_length=_MAX_URL_CHARS)
    title: str = Field(default="", max_length=_MAX_TITLE_CHARS)
    selection: str = Field(default="")
    html: str | None = Field(default=None, max_length=_MAX_HTML_CHARS)


def _read_bookmarklet_source() -> str:
    """Read the static JS that becomes the bookmarklet payload.

    Falls back to an empty IIFE if the file is missing so the page still
    renders (a deploy that lost the static dir shouldn't 500 /bookmarklet).
    """
    try:
        return _BOOKMARKLET_JS_PATH.read_text(encoding="utf-8")
    except OSError:
        log.warning("bookmarklet.source_missing", path=str(_BOOKMARKLET_JS_PATH))
        return "(function(){})();"


def _build_javascript_url(source: str, origin: str) -> str:
    """Inline-minify the IIFE and prefix it with ``javascript:``.

    Strips line comments and collapses whitespace runs — enough to fit a
    bookmark href without pulling in a real JS minifier. The output is
    *not* percent-encoded: modern browsers accept literal spaces and
    quotes inside ``javascript:`` URLs and percent-encoding the body
    would mangle the embedded string literals.
    """
    # Substitute the origin first so the placeholder doesn't get
    # accidentally word-wrapped by the whitespace collapse below.
    body = source.replace("PERSONA_ORIGIN", origin)
    lines: list[str] = []
    for raw_line in body.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("//"):
            continue
        lines.append(stripped)
    joined = " ".join(lines)
    return "javascript:" + joined


@router.get("/bookmarklet", response_class=HTMLResponse)
async def bookmarklet_page(request: Request) -> HTMLResponse:
    """Render the setup page with the drag-to-bookmarks link + raw source."""
    source = _read_bookmarklet_source()
    # request.base_url ends with a slash; strip it so we don't emit
    # "http://localhost:8000//api/bookmarklet/capture" inside the JS.
    origin = str(request.base_url).rstrip("/")
    javascript_url = _build_javascript_url(source, origin)
    return templates.TemplateResponse(
        request,
        "bookmarklet.html",
        {
            "title": "Bookmarklet",
            "active_nav": "settings",
            "javascript_url": javascript_url,
            "raw_source": source,
            "origin": origin,
            "max_selection_chars": _MAX_SELECTION_CHARS,
        },
    )


@router.options("/api/bookmarklet/capture")
async def capture_preflight() -> Response:
    """CORS preflight for the cross-origin POST below."""
    return Response(status_code=204, headers=dict(_CORS_HEADERS))


@router.post("/api/bookmarklet/capture")
async def capture(payload: CapturePayload) -> JSONResponse:
    """Persist the captured page as a standalone inbox note.

    Body shape: ``# {title}\\n\\n{url}\\n\\n> {selection}`` — selection is
    quoted with a leading ``> `` so it renders as a blockquote in any
    markdown viewer. Empty selections collapse to just the URL line.
    """
    url = payload.url.strip()
    if not url:
        return JSONResponse(
            {"ok": False, "error": "url is required"},
            status_code=400,
            headers=dict(_CORS_HEADERS),
        )

    title = payload.title.strip() or url
    selection = payload.selection.replace("\r\n", "\n").strip()
    if len(selection) > _MAX_SELECTION_CHARS:
        selection = selection[:_MAX_SELECTION_CHARS]

    parts: list[str] = [f"# {title}", "", url]
    if selection:
        # Prefix every line with "> " so multi-line selections all land
        # inside the same blockquote when the note is rendered later.
        quoted = "\n".join(f"> {line}" if line else ">" for line in selection.split("\n"))
        parts.extend(["", quoted])
    body = "\n".join(parts)

    async with get_connection() as conn:
        note_id = await insert_inbox_note(
            conn,
            body=body,
            title=title[:_MAX_TITLE_CHARS],
            source="bookmarklet",
        )

    log.info(
        "bookmarklet.captured",
        note_id=note_id,
        url=url,
        title_len=len(title),
        selection_len=len(selection),
    )
    return JSONResponse(
        {"ok": True, "note_id": note_id},
        headers=dict(_CORS_HEADERS),
    )
