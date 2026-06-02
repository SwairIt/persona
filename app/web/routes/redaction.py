"""Admin UI for regex-based OCR text redaction rules.

Lets the user define, toggle, and delete patterns that mask sensitive
substrings (emails, credit-card numbers, bearer tokens, …) inside OCR
text *before* it lands in the database or FTS index. The original
screenshot image is never touched — this only affects the searchable
text representation.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.redaction import create_rule, delete_rule, list_rules, toggle_rule
from app.web.templates_engine import templates

router = APIRouter(tags=["redaction"])


@router.get("/settings/redaction", response_class=HTMLResponse)
async def redaction_page(request: Request) -> HTMLResponse:
    rules = await list_rules()
    return templates.TemplateResponse(
        request,
        "redaction.html",
        {
            "title": "OCR redaction",
            "active_nav": "settings",
            "rules": rules,
        },
    )


@router.post("/settings/redaction")
async def redaction_create(
    name: str = Form(...),
    pattern: str = Form(...),
) -> RedirectResponse:
    try:
        await create_rule(name=name, pattern=pattern)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/redaction", status_code=303)


@router.post("/settings/redaction/{name}/toggle")
async def redaction_toggle(name: str) -> RedirectResponse:
    await toggle_rule(name)
    return RedirectResponse(url="/settings/redaction", status_code=303)


@router.post("/settings/redaction/{name}/delete")
async def redaction_delete(name: str) -> RedirectResponse:
    await delete_rule(name)
    return RedirectResponse(url="/settings/redaction", status_code=303)
