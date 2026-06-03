"""Admin UI for tag aliases — many human-friendly names → one canonical tag.

GET /settings/tag-aliases renders the existing alias table plus a list
of every canonical tag already in use so the operator can spot good
merge targets without scrolling the full ``tags`` page.

POST /settings/tag-aliases upserts one alias and redirects (303 → PRG)
so a refresh doesn't re-submit. POST /settings/tag-aliases/delete drops
one row. Both use the same 303 redirect pattern as the sibling app-name
aliases admin (:mod:`app.web.routes.app_aliases`) so behaviour is
consistent across the settings section.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.tag_aliases import delete, list_all, set_alias
from app.web.templates_engine import templates

log = get_logger("persona.tag_aliases")

router = APIRouter(tags=["tag-aliases"])

# How many canonical tag candidates we surface alongside the editor.
# The page is otherwise a long table of already-aliased rows; this is
# the "next merge targets" list the operator can actually scan. Matches
# the constant used by the app-name aliases admin.
_CANONICAL_LIMIT = 64


@router.get("/settings/tag-aliases", response_class=HTMLResponse)
async def tag_aliases_page(request: Request) -> HTMLResponse:
    """Render the tag alias management page.

    Pulls every configured alias and the most-used tag names from the
    ``tags`` join table so the operator can pick a canonical target
    without flipping back to /tags.
    """
    aliases = await list_all()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT t.name AS name, COUNT(st.screenshot_id) AS n "
            "FROM tags t "
            "LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
            "GROUP BY t.id, t.name "
            "ORDER BY n DESC, t.name ASC "
            "LIMIT ?",
            (_CANONICAL_LIMIT,),
        )
        rows = await cursor.fetchall()
    canonical_candidates = [
        {"name": str(row["name"]), "count": int(row["n"])}
        for row in rows
        if str(row["name"]).strip()
    ]
    return templates.TemplateResponse(
        request,
        "tag_aliases_admin.html",
        {
            "title": "Tag aliases",
            "active_nav": "settings",
            "aliases": aliases,
            "canonical_candidates": canonical_candidates,
        },
    )


@router.post("/settings/tag-aliases")
async def tag_aliases_save(
    alias: str = Form(...),
    canonical: str = Form(...),
) -> RedirectResponse:
    """Upsert a single alias mapping and redirect (303) back to the page.

    An empty ``canonical`` is interpreted as "drop the mapping" by the
    helper (which routes through :func:`app.tag_aliases.delete`), so the
    same form action serves both Save and Clear from the inline editor.
    """
    try:
        await set_alias(alias, canonical)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/tag-aliases", status_code=303)


@router.post("/settings/tag-aliases/delete")
async def tag_aliases_delete(
    alias: str = Form(...),
) -> RedirectResponse:
    """Drop one alias row. Idempotent — a missing row is a no-op."""
    await delete(alias)
    return RedirectResponse(url="/settings/tag-aliases", status_code=303)
