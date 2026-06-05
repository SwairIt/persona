"""HTTP endpoints exposing :mod:`app.chrono_parse`.

Two entry points:

* ``GET /api/chrono/parse?text=...`` — JSON; returns the raw
  :class:`~app.chrono_parse.ChronoRange` dict or ``null``.
* ``GET /api/chrono/preview?text=...`` — HTML; renders a small chip
  the /ask UI can embed live as the user types.

The router is mounted onto :mod:`app.web.routes.qa` so it ships
alongside the existing Q&A endpoints without touching
``app.web.main`` (per project rules).
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.chrono_parse import parse_natural_date
from app.logging_setup import get_logger

log = get_logger("persona.web.chrono_parse")

router = APIRouter(tags=["chrono"])


@router.get("/api/chrono/parse", response_class=JSONResponse)
async def chrono_parse_endpoint(
    text: str = Query(default="", description="Free-form user prompt"),
) -> JSONResponse:
    """Return a :class:`ChronoRange` dict for ``text`` or ``null``."""
    now = datetime.now(tz=UTC)
    hit = parse_natural_date(text, now=now)
    return JSONResponse(content=dict(hit) if hit is not None else None)


@router.get("/api/chrono/preview", response_class=HTMLResponse)
async def chrono_preview_endpoint(
    text: str = Query(default="", description="Free-form user prompt"),
) -> HTMLResponse:
    """Render a small chip describing the matched date range.

    Returns an empty body (``204``-style) when no phrase matched, so the
    UI can swap the chip out via HTMX ``hx-swap-oob`` without leaving
    stale markup behind.
    """
    now = datetime.now(tz=UTC)
    hit = parse_natural_date(text, now=now)
    if hit is None:
        return HTMLResponse(content="", status_code=200)

    phrase = escape(hit["matched_phrase"])
    start = escape(hit["start_iso"][:10])
    end = escape(hit["end_iso"][:10])
    kind = escape(hit["kind"])
    range_text = start if start == end else f"{start} .. {end}"

    html = (
        '<span class="chrono-chip" '
        f'data-kind="{kind}" '
        'style="display:inline-flex;align-items:center;gap:.35em;'
        "padding:.15em .55em;border-radius:999px;"
        "background:#1f2937;color:#e5e7eb;font:500 12px/1.4 system-ui;"
        '">'
        f'<strong style="font-weight:600">{phrase}</strong>'
        f'<span style="opacity:.75">→ {range_text}</span>'
        "</span>"
    )
    return HTMLResponse(content=html)


__all__ = ["router"]
