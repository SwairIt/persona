"""Render the OCR-text diff page for two screenshots.

Persona v0.34 feature 2/3. Sister to :mod:`app.web.routes.diff_slider`
(image slider) and :mod:`app.web.routes.analysis` (token-bag diff):
this view shows a proper ``unified_diff`` + side-by-side HtmlDiff table
of the *OCR text* extracted from each screenshot.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.ocr_diff import ocr_diff
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

log = get_logger("persona.ocr_diff")

router = APIRouter(tags=["analysis"])


@router.get("/diff/ocr/{id_a}/{id_b}", response_class=HTMLResponse)
async def ocr_diff_page(
    request: Request,
    id_a: int,
    id_b: int,
) -> HTMLResponse:
    """Render the OCR diff view for screenshots ``id_a`` and ``id_b``.

    Returns 404 when either id is missing — the page is only useful when
    both sides exist, so refuse to half-render with a placeholder.
    """
    async with get_connection() as conn:
        shot_a = await get_screenshot(conn, id_a)
        shot_b = await get_screenshot(conn, id_b)

    if shot_a is None or shot_b is None:
        missing = [i for i, s in ((id_a, shot_a), (id_b, shot_b)) if s is None]
        log.info("ocr_diff.not_found", missing=missing)
        raise HTTPException(status_code=404, detail="Screenshot not found")

    label_a = f"#{shot_a.id} ({shot_a.app_name or 'unknown'})"
    label_b = f"#{shot_b.id} ({shot_b.app_name or 'unknown'})"
    result = ocr_diff(
        shot_a.ocr_text,
        shot_b.ocr_text,
        label_a=label_a,
        label_b=label_b,
    )

    log.info(
        "ocr_diff.render",
        id_a=id_a,
        id_b=id_b,
        identical=result.identical,
        unified_lines=len(result.unified),
    )

    return templates.TemplateResponse(
        request,
        "ocr_diff.html",
        {
            "title": f"OCR diff #{id_a} vs #{id_b}",
            "active_nav": "timeline",
            "shot_a": shot_a,
            "shot_b": shot_b,
            "diff": result,
        },
    )
