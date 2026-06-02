"""Notes attached to individual screenshots."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.storage.db import get_connection
from app.storage.notes import delete_note, upsert_note
from app.storage.repository import get_screenshot

router = APIRouter(prefix="/api/screenshots", tags=["notes"])


@router.post("/{screenshot_id}/note", response_class=JSONResponse)
async def save_note(screenshot_id: int, body: str = Form(...)) -> JSONResponse:
    async with get_connection() as conn:
        existing = await get_screenshot(conn, screenshot_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        text = body.strip()
        if not text:
            await delete_note(conn, screenshot_id)
        else:
            await upsert_note(conn, screenshot_id, text)
    return JSONResponse({"screenshot_id": screenshot_id, "note": text})


@router.delete("/{screenshot_id}/note", response_class=JSONResponse)
async def remove_note(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        await delete_note(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "deleted": True})
