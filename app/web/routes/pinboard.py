"""Pinboard routes — masonry page, HTMX page fragment and JSON view.

The HTML page (``/pinboard``) renders a Pinterest-style fluid grid of
every pinned screenshot using CSS columns; the HTMX endpoint
(``/pinboard?offset=N&hx=1``) returns just the next page of cards so
the bottom-of-page sentinel can keep loading content without a full
re-render; the JSON endpoint (``/api/pinboard.json``) exposes the same
payload for external tooling.

All three views share :func:`app.pinboard.list_pinned` as the single
source of truth — they cannot drift apart.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.pinboard import list_pinned
from app.web.templates_engine import templates

log = get_logger("persona.pinboard.route")

router = APIRouter(tags=["pinboard"])


# Default page size mirrors the spec — 100 rows is enough to fill a
# desktop viewport on the first paint while staying small enough that
# the SQLite scan + JSON serialisation finish in tens of milliseconds.
_DEFAULT_LIMIT = 100


@router.get("/pinboard", response_class=HTMLResponse)
async def pinboard_page(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=500),
    tag: str | None = Query(default=None),
    hx: int = Query(default=0, ge=0, le=1),
) -> HTMLResponse:
    """Render the masonry page or just the next page fragment for HTMX.

    Args:
        request: FastAPI request — needed by Jinja2Templates.
        offset: Row offset for pagination. Passed through to the
            data layer which clamps it to ``>= 0``.
        limit: Page size. Clamped to ``[1, 500]`` by both FastAPI's
            ``Query(le=500)`` and the data layer for defence-in-depth.
        tag: Optional tag filter. ``None`` or empty string returns
            every pin.
        hx: Internal flag — ``1`` when the call originated from the
            HTMX sentinel and only the inner cards fragment should be
            returned. Defaults to ``0`` (full page).

    Returns:
        ``pinboard.html`` for a fresh page load, or
        ``_pinboard_page.html`` for an HTMX append.
    """
    payload = await list_pinned(limit=limit, offset=offset, tag=tag)

    log.info(
        "pinboard.page",
        offset=payload["offset"],
        limit=payload["limit"],
        items=len(payload["items"]),
        total=payload["total"],
        tag=tag,
        has_more=payload["has_more"],
        hx=bool(hx),
    )

    next_offset = payload["offset"] + payload["limit"]
    context = {
        "title": "Pinboard",
        "active_nav": "memory",
        "items": payload["items"],
        "total": payload["total"],
        "offset": payload["offset"],
        "limit": payload["limit"],
        "has_more": payload["has_more"],
        "next_offset": next_offset,
        "tag": tag,
    }

    template_name = "_pinboard_page.html" if hx else "pinboard.html"
    return templates.TemplateResponse(request, template_name, context)


@router.get("/api/pinboard.json", response_class=JSONResponse)
async def pinboard_json(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=500),
    tag: str | None = Query(default=None),
) -> JSONResponse:
    """Paginated JSON view of every pinned shot, newest-pin-first."""
    payload = await list_pinned(limit=limit, offset=offset, tag=tag)

    log.info(
        "pinboard.json",
        offset=payload["offset"],
        limit=payload["limit"],
        items=len(payload["items"]),
        total=payload["total"],
        tag=tag,
        has_more=payload["has_more"],
    )

    return JSONResponse(
        {
            "items": payload["items"],
            "total": payload["total"],
            "offset": payload["offset"],
            "limit": payload["limit"],
            "has_more": payload["has_more"],
        }
    )


__all__ = ["router"]
