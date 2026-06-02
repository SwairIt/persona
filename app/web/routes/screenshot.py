"""Screenshot detail view."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.notes import get_note
from app.storage.reading_list import is_in_reading_list
from app.storage.repository import get_neighbour_ids, get_screenshot, list_screenshots
from app.storage.tags import get_screenshot_tags, list_tags
from app.web.templates_engine import templates

router = APIRouter(tags=["screenshot"])


@router.get("/screenshot/{screenshot_id}", response_class=HTMLResponse)
async def screenshot_detail(request: Request, screenshot_id: int) -> HTMLResponse:
    """Render detail view for one captured frame."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")

        neighbours = await list_screenshots(
            conn,
            limit=20,
            since=shot.captured_at - timedelta(minutes=15),
            until=shot.captured_at + timedelta(minutes=15),
        )
        note = await get_note(conn, screenshot_id)
        attached_tags = await get_screenshot_tags(conn, screenshot_id)
        all_tags = await list_tags(conn)
        in_reading = await is_in_reading_list(conn, screenshot_id)
        prev_id, next_id = await get_neighbour_ids(conn, screenshot_id=screenshot_id)

    return templates.TemplateResponse(
        request,
        "screenshot.html",
        {
            "title": f"Screenshot #{screenshot_id}",
            "active_nav": "timeline",
            "shot": shot,
            "neighbours": neighbours,
            "note": note or "",
            "attached_tags": attached_tags,
            "all_tags": all_tags,
            "in_reading_list": in_reading,
            "prev_id": prev_id,
            "next_id": next_id,
        },
    )
