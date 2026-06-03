"""HTTP surface for the OCR-via-vision fallback.

Two surfaces:

* ``POST /api/screenshot/{id}/ocr-vision`` — trigger the fallback for a
  single shot and return the resulting status + text as JSON.
* ``GET  /admin/ocr-vision`` — admin page listing low-confidence /
  empty-text shots with a one-click button per row to fire the API
  endpoint via ``fetch``.

The admin page reuses :mod:`app.ocr_retry` to surface the candidate
list — there's no point duplicating the SQL-and-Jaccard logic that
module already does well. The "trigger" button is purely client-side
``fetch`` + reload; no HTMX dependency, no Alpine state machine —
keeping the surface area minimal.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.llm.ocr_via_vision import extract_text_via_vision
from app.logging_setup import get_logger
from app.ocr_retry import (
    DEFAULT_MIN_CONF,
    count_problem_shots,
    list_problem_shots,
)
from app.settings import get_settings
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-vision"])
log = get_logger("persona.ocr.vision")

# Page-cap is intentionally tighter than ``/admin/ocr-retry`` because
# each vision call is markedly more expensive (full image upload +
# multimodal completion) — surfacing 200 rows would tempt the user
# into firing 200 paid completions in a row.
PAGE_LIMIT: int = 50


@router.post(
    "/api/screenshot/{screenshot_id}/ocr-vision",
    response_class=JSONResponse,
)
async def ocr_vision_trigger(screenshot_id: int) -> JSONResponse:
    """Trigger the vision fallback for ``screenshot_id``.

    The underlying helper is tolerant — it returns a status string
    rather than raising on configuration / network problems — so this
    endpoint can pass the result straight through to the client. The
    only thing we add is the ``screenshot_id`` echo for the JS layer.
    """
    result = await extract_text_via_vision(screenshot_id)
    log.info(
        "ocr.vision.route.trigger",
        shot_id=screenshot_id,
        status=result["status"],
        chars=len(result["text"]),
    )
    return JSONResponse(
        {
            "screenshot_id": screenshot_id,
            "status": result["status"],
            "text": result["text"],
        }
    )


@router.get("/admin/ocr-vision", response_class=HTMLResponse)
async def ocr_vision_admin_page(request: Request) -> HTMLResponse:
    """Render the admin dashboard listing low-confidence shots.

    Surfaces the same ``empty``/``low_conf`` rows the OCR retry page
    does, but the action button per row triggers the multimodal vision
    fallback instead of re-queuing Tesseract.
    """
    settings = get_settings()
    rows = await list_problem_shots(
        limit=PAGE_LIMIT,
        min_conf=DEFAULT_MIN_CONF,
        only_empty=False,
        only_low=False,
    )
    total = await count_problem_shots(
        min_conf=DEFAULT_MIN_CONF,
        only_empty=False,
        only_low=False,
    )
    provider = (settings.byo_api_provider or "").strip().lower()
    enabled = settings.llm_vision_enabled and provider == "anthropic" and bool(
        settings.byo_api_key.strip()
    )
    return templates.TemplateResponse(
        request,
        "ocr_vision_admin.html",
        {
            "title": "OCR via vision",
            "active_nav": "settings",
            "rows": rows,
            "total": total,
            "shown": len(rows),
            "page_limit": PAGE_LIMIT,
            "min_conf": DEFAULT_MIN_CONF,
            "vision_enabled": settings.llm_vision_enabled,
            "provider": provider or "(unset)",
            "feature_ready": enabled,
        },
    )
