"""Admin UI + API for regex auto-tag rules."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.regex_rules import (
    create_rule,
    delete_rule,
    list_rules,
    toggle_rule,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["regex-rules"])


@router.get("/regex-rules", response_class=HTMLResponse)
async def regex_rules_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        rules = await list_rules(conn)
    return templates.TemplateResponse(
        request,
        "regex_rules.html",
        {
            "title": "Regex auto-tag rules",
            "active_nav": "settings",
            "rules": rules,
        },
    )


@router.post("/regex-rules")
async def regex_rules_create(
    pattern: str = Form(...),
    tag_name: str = Form(...),
    case_insensitive: str | None = Form(default=None),
) -> RedirectResponse:
    ci = case_insensitive is not None
    async with get_connection() as conn:
        try:
            await create_rule(
                conn,
                pattern=pattern,
                tag_name=tag_name,
                case_insensitive=ci,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/regex-rules", status_code=303)


@router.post("/regex-rules/{rule_id}/toggle")
async def regex_rules_toggle(
    rule_id: int,
    enabled: str | None = Form(default=None),
) -> RedirectResponse:
    flag = enabled is not None and enabled.lower() in {"1", "true", "on", "yes"}
    async with get_connection() as conn:
        await toggle_rule(conn, rule_id, flag)
    return RedirectResponse(url="/regex-rules", status_code=303)


@router.post("/regex-rules/{rule_id}/delete")
async def regex_rules_delete(rule_id: int) -> RedirectResponse:
    async with get_connection() as conn:
        await delete_rule(conn, rule_id)
    return RedirectResponse(url="/regex-rules", status_code=303)


@router.post("/api/regex-rules/test", response_class=JSONResponse)
async def regex_rules_test(
    pattern: str = Form(...),
    text: str = Form(default=""),
    case_insensitive: str | None = Form(default=None),
) -> JSONResponse:
    """Live preview helper — returns up to 10 substrings matched by the pattern."""
    ci = case_insensitive is not None and case_insensitive.lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    try:
        regex = re.compile(pattern, re.IGNORECASE if ci else 0)
    except re.error as exc:
        return JSONResponse({"matches": [], "error": str(exc)}, status_code=200)

    matches: list[str] = []
    for m in regex.finditer(text or ""):
        matches.append(m.group(0))
        if len(matches) >= 10:
            break
    return JSONResponse({"matches": matches})
