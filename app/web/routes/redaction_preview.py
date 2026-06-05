"""Interactive PII-filter preview UI.

Users land on ``/settings/redaction/preview``, paste arbitrary text into
the textarea, and immediately see what the currently-enabled redaction
rules would strip — without polluting the real OCR ``screenshots``
table or the search index. The POST endpoint takes a JSON body so the
HTMX call from the page (and any future automation) does not have to
URL-encode multi-line text bodies.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.redaction_preview import MAX_SAMPLE_CHARS, preview_redactions
from app.web.templates_engine import templates

log = get_logger("persona.redaction_preview.routes")

router = APIRouter(tags=["redaction_preview"])


def _coerce_sample_text(payload: object) -> str:
    """Pull ``sample_text`` out of a parsed JSON body or raise ``HTTPException``.

    Centralised so the POST handler stays small and the front-end gets a
    crisp 400 with a one-line diagnostic instead of a stack trace.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    sample_text = payload.get("sample_text")
    if sample_text is None:
        # Empty paste is fine — we still want to render an empty pane —
        # but a missing key is a client bug worth surfacing loudly.
        raise HTTPException(
            status_code=400,
            detail="body must contain a 'sample_text' string",
        )
    if not isinstance(sample_text, str):
        raise HTTPException(
            status_code=400,
            detail="'sample_text' must be a string",
        )
    return sample_text


@router.get("/settings/redaction/preview", response_class=HTMLResponse)
async def redaction_preview_page(request: Request) -> HTMLResponse:
    """Render the live PII-filter test page."""
    return templates.TemplateResponse(
        request,
        "redaction_preview.html",
        {
            "title": "Тест PII фильтров",
            "active_nav": "settings",
            "max_sample_chars": MAX_SAMPLE_CHARS,
        },
    )


@router.post("/api/redaction/preview", response_class=JSONResponse)
async def redaction_preview_api(request: Request) -> JSONResponse:
    """Apply enabled rules to ``sample_text`` and return the result."""
    try:
        body: object = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    sample_text = _coerce_sample_text(body)
    result: dict[str, Any] = await preview_redactions(sample_text)
    return JSONResponse(result)
