"""Admin UI for OCR phrase-based auto-tag rules."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.ocr_phrase_tags import add, delete, list_rules
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-phrase-tags"])


@router.get("/settings/phrase-tags", response_class=HTMLResponse)
async def phrase_tags_page(request: Request) -> HTMLResponse:
    rules = await list_rules()
    return templates.TemplateResponse(
        request,
        "phrase_tags.html",
        {
            "title": "OCR phrase auto-tags",
            "active_nav": "settings",
            "rules": rules,
        },
    )


@router.post("/settings/phrase-tags")
async def phrase_tags_create(
    phrase: str = Form(...),
    tag: str = Form(...),
    case_sensitive: str | None = Form(default=None),
) -> RedirectResponse:
    cs = case_sensitive is not None and case_sensitive.lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    try:
        await add(phrase=phrase, tag=tag, case_sensitive=cs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/phrase-tags", status_code=303)


@router.post("/settings/phrase-tags/{rule_id}/delete")
async def phrase_tags_delete(rule_id: int) -> RedirectResponse:
    await delete(rule_id)
    return RedirectResponse(url="/settings/phrase-tags", status_code=303)
