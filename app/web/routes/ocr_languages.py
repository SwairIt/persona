"""Settings UI for the Tesseract language list.

Lets the user pick which language packs the OCR worker should activate
on every capture. Selections are validated against the languages
actually installed on the host so the worker never tries to invoke a
non-existent pack.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.ocr.languages import (
    get_configured_languages,
    get_installed_languages,
    set_configured_languages,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr"])
logger = get_logger("persona.ocr.languages")


@router.get("/settings/ocr-languages", response_class=HTMLResponse)
async def ocr_languages_page(request: Request) -> HTMLResponse:
    """Render the language-picker page."""
    installed = await get_installed_languages()
    configured = await get_configured_languages()
    return templates.TemplateResponse(
        request,
        "ocr_languages.html",
        {
            "title": "OCR languages",
            "active_nav": "settings",
            "installed": installed,
            "configured": configured,
            "configured_set": set(configured),
            "current_string": "+".join(configured),
        },
    )


@router.post("/settings/ocr-languages")
async def ocr_languages_save(request: Request) -> RedirectResponse:
    """Persist the user's language selection.

    Reads the form manually so the repeated ``langs`` field
    (``<input type="checkbox" name="langs" value="eng">`` etc.) is
    collected as a list - FastAPI's ``Form(...)`` only returns the first
    value.
    """
    form = await request.form()
    raw_values = form.getlist("langs")
    langs = [str(value) for value in raw_values]
    try:
        await set_configured_languages(langs)
    except ValueError as exc:
        logger.warning("ocr.languages.save.rejected", error=str(exc), langs=langs)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/ocr-languages", status_code=303)
