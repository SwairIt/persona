"""Admin UI for app-name display aliases — rename ``devenv.exe`` → ``Visual Studio``.

GET /settings/app-aliases renders the existing alias table plus a
"suggested" list of the most-captured raw ``app_name`` values that do
not yet have an alias. POST /settings/app-aliases upserts one alias and
redirects back to the page (303 → standard PRG pattern, so a refresh
doesn't re-submit).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.app_aliases import list_all, set_alias
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.app_aliases")

router = APIRouter(tags=["app-aliases"])

# How many distinct raw ``app_name`` values we surface as inline-rename
# suggestions. The page is otherwise a long table of already-aliased
# rows; we want a manageable list of "next candidates" the operator can
# actually scan. 64 matches what app_overrides shows and lines up with
# one screen on a typical laptop.
_SUGGESTION_LIMIT = 64


@router.get("/settings/app-aliases", response_class=HTMLResponse)
async def aliases_page(request: Request) -> HTMLResponse:
    """Render the alias management page.

    Pulls every configured alias and the top distinct ``app_name`` values
    from ``screenshots``. The "suggested" list filters out names that
    already have an alias so the operator never sees a duplicate row.
    """
    aliases = await list_all()
    aliased_originals = {item["original_name"] for item in aliases}
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT ?",
            (_SUGGESTION_LIMIT,),
        )
        rows = await cursor.fetchall()
    suggested = [
        {"app_name": str(row["app_name"]), "count": int(row["n"])}
        for row in rows
        if str(row["app_name"]) not in aliased_originals
    ]
    return templates.TemplateResponse(
        request,
        "app_aliases.html",
        {
            "title": "App name aliases",
            "active_nav": "settings",
            "aliases": aliases,
            "suggested": suggested,
        },
    )


@router.post("/settings/app-aliases")
async def aliases_save(
    original_name: str = Form(...),
    display_name: str = Form(...),
) -> RedirectResponse:
    """Upsert a single alias and redirect (303) back to the page."""
    try:
        await set_alias(original_name, display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/app-aliases", status_code=303)
