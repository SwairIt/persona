"""Side-by-side compare view + JSON endpoint backed by :mod:`app.shot_compare`.

Routes:

* ``GET /compare?a=<id>&b=<id>`` — HTML page with both thumbnails and a
  per-block coloured OCR diff. Active nav: ``timeline``.
* ``GET /api/compare.json?a=<id>&b=<id>`` — same payload as a JSON dict
  so external tooling can consume the diff without scraping HTML.

Both endpoints return 404 when either id is missing from the database;
the helper raises :class:`LookupError` and we translate that here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.shot_compare import (
    CompareResult,
    compare_shots,
    find_previous_shot_id,
)
from app.web.templates_engine import templates

log = get_logger("persona.shot_compare.web")

router = APIRouter(tags=["analysis"])


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(
    request: Request,
    _user: Annotated[SessionRecord, Depends(current_user_required)],
    a: int = Query(..., description="ID of the 'before' screenshot"),
    b: int = Query(..., description="ID of the 'after' screenshot"),
) -> HTMLResponse:
    """Render the side-by-side comparison page for shots ``a`` and ``b``."""
    result = await _load_or_404(a, b)

    # Best-effort: surface a "view previous from same app" CTA when the
    # user landed on /compare directly. We compute it from ``b`` (the
    # newer side) so the CTA jumps further back in history rather than
    # re-targeting the diff at the same older shot.
    prev_of_b = await find_previous_shot_id(b)

    log.info("shot_compare.page", a=a, b=b, prev_of_b=prev_of_b)

    return templates.TemplateResponse(
        request,
        "shot_compare.html",
        {
            "title": f"Compare #{a} vs #{b}",
            "active_nav": "timeline",
            "result": result,
            "prev_of_b": prev_of_b,
        },
    )


@router.get("/api/compare.json")
async def compare_json(
    _user: Annotated[SessionRecord, Depends(current_user_required)],
    a: int = Query(..., description="ID of the 'before' screenshot"),
    b: int = Query(..., description="ID of the 'after' screenshot"),
) -> JSONResponse:
    """JSON twin of :func:`compare_page` — returns the raw compare result."""
    result = await _load_or_404(a, b)
    return JSONResponse(content=dict(result))


async def _load_or_404(a: int, b: int) -> CompareResult:
    """Run :func:`compare_shots` and translate ``LookupError`` to HTTP 404."""
    try:
        return await compare_shots(a, b)
    except LookupError as exc:
        log.info("shot_compare.http_404", a=a, b=b, error=str(exc))
        raise HTTPException(status_code=404, detail="Screenshot not found") from exc
