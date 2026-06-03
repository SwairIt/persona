"""Pin-map routes — HTML page and JSON view of every pinned screenshot.

The HTML page renders one section per ``YYYY-MM`` cluster with a 4-column grid
of 240-pixel thumbnails; the JSON endpoint exposes the same payload for
external tooling. Both views share :func:`app.pinmap.build_pinmap` as the
single source of truth.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.pinmap import build_pinmap
from app.web.templates_engine import templates

log = get_logger("persona.pinmap")

router = APIRouter(tags=["pinmap"])


@router.get("/pinmap", response_class=HTMLResponse)
async def pinmap_page(request: Request) -> HTMLResponse:
    """Render the pin-map page — every pinned shot on a single scrollable grid."""
    payload = await build_pinmap()
    log.info(
        "pinmap.page",
        total=payload["total"],
        cluster_count=len(payload["clusters"]),
    )
    return templates.TemplateResponse(
        request,
        "pinmap.html",
        {
            "title": "Pin map",
            "active_nav": "timeline",
            "clusters": payload["clusters"],
            "total": payload["total"],
        },
    )


@router.get("/api/pinmap.json", response_class=JSONResponse)
async def pinmap_json() -> JSONResponse:
    """JSON view of every pinned screenshot grouped by capture month."""
    payload = await build_pinmap()
    log.info(
        "pinmap.json",
        total=payload["total"],
        cluster_count=len(payload["clusters"]),
    )
    return JSONResponse(
        {
            "clusters": payload["clusters"],
            "total": payload["total"],
        }
    )
