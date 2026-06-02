"""Web UI + API for reusable note templates."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.note_templates import (
    create_template,
    delete_template,
    get_template,
    list_templates,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["note-templates"])


@router.get("/notes/templates", response_class=HTMLResponse)
async def note_templates_page(request: Request) -> HTMLResponse:
    async with get_connection() as conn:
        items = await list_templates(conn)
    return templates.TemplateResponse(
        request,
        "note_templates.html",
        {
            "title": "Note templates",
            "active_nav": "settings",
            "items": items,
        },
    )


@router.post("/notes/templates")
async def note_templates_create(
    slug: str = Form(...),
    title: str = Form(...),
    body: str = Form(...),
) -> RedirectResponse:
    async with get_connection() as conn:
        try:
            await create_template(conn, slug=slug, title=title, body=body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/notes/templates", status_code=303)


@router.post("/notes/templates/{slug}/delete")
async def note_templates_delete(slug: str) -> RedirectResponse:
    async with get_connection() as conn:
        try:
            await delete_template(conn, slug)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/notes/templates", status_code=303)


@router.get("/notes/templates/{slug}/apply", response_class=PlainTextResponse)
async def note_templates_apply(slug: str) -> PlainTextResponse:
    """Return the template body as plain text for the frontend to paste."""
    async with get_connection() as conn:
        try:
            tpl = await get_template(conn, slug)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if tpl is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return PlainTextResponse(tpl["body"])
