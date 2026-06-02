"""Reading list — save screenshots for "review later"."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.storage.db import get_connection
from app.storage.reading_list import (
    add_to_reading_list,
    count_unread,
    is_in_reading_list,
    list_reading_list,
    mark_read,
    remove_from_reading_list,
)
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["reading-list"])


@router.get("/reading", response_class=HTMLResponse)
async def reading_page(
    request: Request,
    show_read: bool = Query(default=False),
) -> HTMLResponse:
    async with get_connection() as conn:
        shots = await list_reading_list(conn, include_read=show_read)
        unread = await count_unread(conn)
    return templates.TemplateResponse(
        request,
        "reading.html",
        {
            "title": "Reading list",
            "active_nav": "reading",
            "shots": shots,
            "unread": unread,
            "show_read": show_read,
        },
    )


@router.post("/api/screenshots/{screenshot_id}/read-later", response_class=JSONResponse)
async def add_read_later(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        if (await get_screenshot(conn, screenshot_id)) is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        await add_to_reading_list(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "in_reading_list": True})


@router.delete("/api/screenshots/{screenshot_id}/read-later", response_class=JSONResponse)
async def remove_read_later(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        await remove_from_reading_list(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "in_reading_list": False})


@router.post("/api/screenshots/{screenshot_id}/mark-read", response_class=JSONResponse)
async def mark_read_endpoint(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        if not await is_in_reading_list(conn, screenshot_id):
            raise HTTPException(status_code=404, detail="Not in reading list")
        await mark_read(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "marked_read": True})


@router.get("/api/export/reading.md")
async def export_reading_markdown(include_read: bool = Query(default=False)) -> Response:
    async with get_connection() as conn:
        shots = await list_reading_list(conn, include_read=include_read, limit=1000)

    lines = ["# Persona — Reading list", ""]
    if not shots:
        lines.append("_(empty)_")
    for shot in shots:
        ts = shot.captured_at.strftime("%Y-%m-%d %H:%M")
        lines.append(f"## [{ts}] {shot.app_name or '—'}")
        if shot.window_title:
            lines.append(f"_{shot.window_title}_")
        lines.append("")
        if shot.ocr_text:
            snippet = shot.ocr_text[:400].strip().replace("\n", " ")
            lines.append("> " + snippet)
        lines.append(f"[screenshot #{shot.id}](/screenshot/{shot.id})")
        lines.append("")
    body = "\n".join(lines)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="persona-reading-list.md"'},
    )
