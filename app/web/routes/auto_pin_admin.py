"""Admin UI for the regex-based auto-pin rules (v1.27).

CRUD on ``auto_pin_rule`` — patterns whose ``re.search`` match against
``screenshots.ocr_text`` causes the auto-pin worker to flip
``pinned_at`` for the matching shot.

Routes:

    GET  /settings/auto-pin                  — list rules + add form
    POST /settings/auto-pin                  — insert a new rule
    POST /settings/auto-pin/{id}/delete      — drop a rule (+ its watermark)
    POST /settings/auto-pin/{id}/toggle      — flip ``enabled``
    GET  /api/auto-pin/rules.json            — JSON dump for tooling

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.auto_pin.admin")

router = APIRouter(tags=["auto-pin"])


async def _list_rules(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return every row in ``auto_pin_rule``, including disabled ones."""
    cursor = await conn.execute(
        "SELECT id, pattern, enabled, description, created_at "
        "FROM auto_pin_rule "
        "ORDER BY id DESC",
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "pattern": str(row["pattern"]),
            "enabled": bool(row["enabled"]),
            "description": (
                str(row["description"]) if row["description"] is not None else None
            ),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _validate_pattern(pattern: str) -> str:
    """Strip + validate a user-supplied regex. Raises :class:`ValueError`.

    Empty patterns are rejected — ``re.compile('')`` succeeds and would
    match every OCR text on the first tick, immediately exhausting the
    daily-cap budget. That's exactly the runaway the cap is supposed
    to slow down, not enable.
    """
    stripped = pattern.strip()
    if not stripped:
        msg = "pattern is required"
        raise ValueError(msg)
    try:
        re.compile(stripped)
    except re.error as exc:
        msg = f"invalid regex: {exc}"
        raise ValueError(msg) from exc
    return stripped


@router.get("/settings/auto-pin", response_class=HTMLResponse)
async def auto_pin_page(request: Request) -> HTMLResponse:
    """Render the auto-pin rule management page."""
    async with get_connection() as conn:
        rules = await _list_rules(conn)
    return templates.TemplateResponse(
        request,
        "auto_pin_admin.html",
        {
            "title": "Авто-пин по regex",
            "active_nav": "settings",
            "rules": rules,
        },
    )


@router.post("/settings/auto-pin")
async def auto_pin_create(
    pattern: str = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    """Insert a new rule, then 303-redirect back to the list."""
    try:
        clean_pattern = _validate_pattern(pattern)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    desc_value: str | None = description.strip() or None

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO auto_pin_rule (pattern, description) VALUES (?, ?)",
            (clean_pattern, desc_value),
        )
        await conn.commit()
    log.info(
        "auto_pin.rule_created",
        pattern=clean_pattern,
        has_description=desc_value is not None,
    )
    return RedirectResponse(url="/settings/auto-pin", status_code=303)


@router.post("/settings/auto-pin/{rule_id}/delete")
async def auto_pin_delete(rule_id: int) -> RedirectResponse:
    """Drop a rule + its watermark, then 303-redirect back to the list."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM auto_pin_rule WHERE id = ?",
            (int(rule_id),),
        )
        await conn.execute(
            "DELETE FROM auto_pin_watermark WHERE rule_id = ?",
            (int(rule_id),),
        )
        await conn.commit()
    log.info("auto_pin.rule_deleted", rule_id=int(rule_id))
    return RedirectResponse(url="/settings/auto-pin", status_code=303)


@router.post("/settings/auto-pin/{rule_id}/toggle")
async def auto_pin_toggle(rule_id: int) -> RedirectResponse:
    """Flip ``enabled`` for one rule, then 303-redirect back to the list."""
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE auto_pin_rule SET enabled = 1 - enabled WHERE id = ?",
            (int(rule_id),),
        )
        await conn.commit()
    log.info("auto_pin.rule_toggled", rule_id=int(rule_id))
    return RedirectResponse(url="/settings/auto-pin", status_code=303)


@router.get("/api/auto-pin/rules.json")
async def auto_pin_rules_json() -> JSONResponse:
    """Return every rule as JSON. Includes disabled rows."""
    async with get_connection() as conn:
        rules = await _list_rules(conn)
    return JSONResponse(rules)


__all__ = ["router"]
