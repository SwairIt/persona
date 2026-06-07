"""Drag-to-tag endpoint — apply a tag to a screenshot by name.

Companion to ``app/web/static/drag_to_tag.js`` (v0.41). The browser POSTs
a ``tag`` form field with the tag name (auto-created if it does not yet
exist) so the user can build new tags purely by drag-and-drop, without
visiting /tags first.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import current_user_optional
from app.logging_setup import get_logger
from app.shots import ensure_uuid
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.tags import create_tag, get_screenshot_tags, tag_screenshot
from app.sync import append_event

log = get_logger("persona.drag_to_tag")

router = APIRouter(tags=["tags"])


@router.post("/api/screenshot/{screenshot_id}/tags", response_class=JSONResponse)
async def apply_tag_by_name(
    request: Request,
    screenshot_id: int,
    tag: str = Form(...),
) -> JSONResponse:
    """Apply ``tag`` (auto-created if needed) to ``screenshot_id``.

    Returns the screenshot's full current tag list so the caller can
    refresh its UI without a second round-trip.

    T6 (2026-06-07) — also emits a ``shot_tag`` sync event so the
    attachment fans out to the user's other devices. The event payload
    carries the shot uuid (minted lazily) + tag name; the receiving
    device resolves the tag by uuid first then by name, creating one
    on the fly when nothing matches.
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

    # T6 sync fan-out. Mint the shot uuid lazily so the event carries a
    # stable cross-device identifier; this is the moment the shot first
    # becomes sync-aware. Same for the tag — read its uuid (or mint one
    # via the helper).
    session = await current_user_optional(request)
    if session is not None:
        shot_uuid = await ensure_uuid(screenshot_id)
        if shot_uuid is not None:
            tag_uuid: str | None = None
            try:
                async with get_connection() as conn:
                    cursor = await conn.execute(
                        "SELECT uuid FROM tags WHERE id = ?", (tag_id,)
                    )
                    row = await cursor.fetchone()
                    if row is not None and row["uuid"]:
                        tag_uuid = str(row["uuid"])
                await append_event(
                    user_id=session["user_id"],
                    kind="shot_tag",
                    op="insert",
                    payload={
                        "shot_uuid": shot_uuid,
                        "tag_uuid": tag_uuid,
                        "tag_name": name,
                    },
                )
            except Exception as exc:
                log.warning("drag_to_tag.event_emit_failed", error=str(exc))

    return JSONResponse(
        {
            "screenshot_id": screenshot_id,
            "tag_id": tag_id,
            "tag": name,
            "tags": current,
        }
    )
