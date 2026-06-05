"""Admin UI for the focus-session app whitelist (v1.47).

Inverse of :mod:`app.web.routes.focus_blocklist`. Lets the operator
declare the small set of apps that *belong* to the current deep-work
block — the IDE, Figma, the spec PDF — and everything else is treated
as a distraction while a focus_session is active. An empty whitelist
falls back to "open mode" so a casual user who never visits this page
never sees a behaviour change.

Routes:

    GET  /focus/whitelist                    — table + add form
    POST /focus/whitelist                    — adds one app
    POST /focus/whitelist/{id}/delete        — removes one app by row id
    GET  /api/focus/whitelist.json           — JSON snapshot for the
                                                client clock / external
                                                tooling

The id-based delete URL is deliberate (over a name-based one): it
survives renames, and it lets the form action stay a plain ASCII path
without URL-encoding whatever odd characters an app name carries. All
POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.focus_whitelist import (
    add_app,
    list_apps,
    remove_by_id,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.focus_whitelist")

router = APIRouter(tags=["focus-whitelist"])

# Cap on the datalist suggestion count. Lines up with the sibling
# focus_blocklist page so the two settings pages feel symmetric.
_SUGGESTION_LIMIT = 200


@router.get("/focus/whitelist", response_class=HTMLResponse)
async def focus_whitelist_page(request: Request) -> HTMLResponse:
    """Render the focus-whitelist management page.

    Pulls every whitelisted app and the distinct ``app_name`` values
    from ``screenshots`` for the ``<datalist>`` autocomplete.
    Suggestions that already match an entry in the whitelist are
    filtered out so the operator never sees a duplicate.
    """
    whitelisted = await list_apps()
    whitelisted_set = {item["app_name"].strip().casefold() for item in whitelisted}
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
        if str(row["app_name"]).strip().casefold() not in whitelisted_set
    ]
    return templates.TemplateResponse(
        request,
        "focus_whitelist.html",
        {
            "title": "Focus whitelist",
            "active_nav": "focus",
            "whitelisted": whitelisted,
            "suggestions": suggestions,
        },
    )


@router.post("/focus/whitelist")
async def focus_whitelist_create(app_name: str = Form(...)) -> RedirectResponse:
    """Add ``app_name`` to the whitelist, then redirect (303) back."""
    try:
        await add_app(app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/focus/whitelist", status_code=303)


@router.post("/focus/whitelist/{row_id}/delete")
async def focus_whitelist_delete(row_id: int) -> RedirectResponse:
    """Drop the given whitelist row, then redirect (303) back."""
    await remove_by_id(row_id)
    return RedirectResponse(url="/focus/whitelist", status_code=303)


@router.get("/api/focus/whitelist.json", response_class=JSONResponse)
async def focus_whitelist_json() -> JSONResponse:
    """JSON snapshot of the whitelist for tooling and the client clock."""
    entries = await list_apps()
    return JSONResponse({"whitelist": [dict(entry) for entry in entries]})
