"""Admin UI for the per-app capture pause list.

Lets the operator mark specific apps as "never capture" — the capture
loop short-circuits before persisting any screenshot row when the
foreground window matches. This is the stricter sibling of the OCR
skip-list: the OCR list still keeps the image, this list keeps
nothing at all.

GET  /settings/app-capture-skip            renders the table + add form
POST /settings/app-capture-skip            adds one app
POST /settings/app-capture-skip/{name}/delete  removes one app

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.app_capture_skip import add, list_skipped, remove
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.app_capture_skip")

router = APIRouter(tags=["app-capture-skip"])

# Cap on the datalist suggestion count. The page is otherwise small;
# 200 distinct app names is plenty for an autocomplete and lines up
# with what the OCR skip-list page renders so the two settings pages
# feel symmetric.
_SUGGESTION_LIMIT = 200


@router.get("/settings/app-capture-skip", response_class=HTMLResponse)
async def app_capture_skip_page(request: Request) -> HTMLResponse:
    """Render the capture-pause management page.

    Pulls every paused app and the distinct ``app_name`` values from
    ``screenshots`` for the ``<datalist>`` autocomplete. Suggestions
    that already match an entry in the pause list are filtered out so
    the operator never sees a duplicate.
    """
    skipped = await list_skipped()
    skipped_set = {item.strip().casefold() for item in skipped}
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
        if str(row["app_name"]).strip().casefold() not in skipped_set
    ]
    return templates.TemplateResponse(
        request,
        "app_capture_skip.html",
        {
            "title": "App capture pause",
            "active_nav": "settings",
            "skipped": skipped,
            "suggestions": suggestions,
        },
    )


@router.post("/settings/app-capture-skip")
async def app_capture_skip_create(app_name: str = Form(...)) -> RedirectResponse:
    """Add ``app_name`` to the pause list, then redirect (303) back."""
    try:
        await add(app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/app-capture-skip", status_code=303)


@router.post("/settings/app-capture-skip/{app_name}/delete")
async def app_capture_skip_delete(app_name: str) -> RedirectResponse:
    """Drop ``app_name`` from the pause list, then redirect (303) back."""
    await remove(app_name)
    return RedirectResponse(url="/settings/app-capture-skip", status_code=303)
