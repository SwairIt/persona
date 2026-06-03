"""Admin UI for the focus-session distraction blocker (v0.85).

Lets the operator pick apps the capture loop must ignore while a
:mod:`app.focus` session is running. The block is *conditional* — apps
on this list are captured normally outside an active focus_session;
they're only suppressed during deep-work blocks. This is the opposite
trade-off from :mod:`app.web.routes.app_capture_skip` (always paused)
and from the OCR skip-list (keep image, drop text).

GET  /settings/focus-blocklist                  renders the table + add form
POST /settings/focus-blocklist                  adds one app
POST /settings/focus-blocklist/{name}/delete    removes one app

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.focus_blocklist import add, list_blocked, remove
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.focus.blocklist")

router = APIRouter(tags=["focus-blocklist"])

# Cap on the datalist suggestion count. Lines up with the sibling
# ``app_capture_skip`` page so the two settings pages feel symmetric.
_SUGGESTION_LIMIT = 200


@router.get("/settings/focus-blocklist", response_class=HTMLResponse)
async def focus_blocklist_page(request: Request) -> HTMLResponse:
    """Render the focus-blocklist management page.

    Pulls every blocked app and the distinct ``app_name`` values from
    ``screenshots`` for the ``<datalist>`` autocomplete. Suggestions
    that already match an entry in the blocklist are filtered out so
    the operator never sees a duplicate.
    """
    blocked = await list_blocked()
    blocked_set = {item.strip().casefold() for item in blocked}
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT app_name FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "ORDER BY app_name LIMIT ?",
            (_SUGGESTION_LIMIT,),
        )
        rows = await cursor.fetchall()
    suggestions = [
        str(row["app_name"])
        for row in rows
        if str(row["app_name"]).strip().casefold() not in blocked_set
    ]
    return templates.TemplateResponse(
        request,
        "focus_blocklist.html",
        {
            "title": "Focus blocklist",
            "active_nav": "settings",
            "blocked": blocked,
            "suggestions": suggestions,
        },
    )


@router.post("/settings/focus-blocklist")
async def focus_blocklist_create(app_name: str = Form(...)) -> RedirectResponse:
    """Add ``app_name`` to the blocklist, then redirect (303) back."""
    try:
        await add(app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/focus-blocklist", status_code=303)


@router.post("/settings/focus-blocklist/{app_name}/delete")
async def focus_blocklist_delete(app_name: str) -> RedirectResponse:
    """Drop ``app_name`` from the blocklist, then redirect (303) back."""
    await remove(app_name)
    return RedirectResponse(url="/settings/focus-blocklist", status_code=303)
