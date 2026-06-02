"""Drag-to-tag endpoint — apply a tag to a screenshot by name.

Companion to ``app/web/static/drag_to_tag.js`` (v0.41). The browser POSTs
a ``tag`` form field with the tag name (auto-created if it does not yet
exist) so the user can build new tags purely by drag-and-drop, without
visiting /tags first.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.tags import create_tag, get_screenshot_tags, tag_screenshot

router = APIRouter(tags=["tags"])


@router.post("/api/screenshot/{screenshot_id}/tags", response_class=JSONResponse)
async def apply_tag_by_name(
    screenshot_id: int,
    tag: str = Form(...),
) -> JSONResponse:
    """Apply ``tag`` (auto-created if needed) to ``screenshot_id``.

    Returns the screenshot's full current tag list so the caller can
    refresh its UI without a second round-trip.
    """
    name = tag.strip().lower()
    if not name:
        raise HTTPException(status_code=400, detail="Empty tag name")

    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        tag_id = await create_tag(conn, name=name)
        await tag_screenshot(conn, screenshot_id, tag_id)
        current = await get_screenshot_tags(conn, screenshot_id)

    return JSONResponse(
        {
            "screenshot_id": screenshot_id,
            "tag_id": tag_id,
            "tag": name,
            "tags": current,
        }
    )
