"""LLM-assisted note draft for a screenshot detail page."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.llm import LLMNotConfigured, draft_note
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

router = APIRouter(tags=["note-assist"])


@router.post("/api/screenshots/{screenshot_id}/draft-note", response_class=JSONResponse)
async def draft_note_endpoint(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if shot.is_private:
        raise HTTPException(status_code=400, detail="Cannot draft a note for a private screenshot")
    try:
        text = await draft_note(
            app_name=shot.app_name,
            window_title=shot.window_title,
            ocr_text=shot.ocr_text,
        )
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"screenshot_id": screenshot_id, "draft": text})
