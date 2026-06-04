"""Pin / unpin screenshots so they're never demoted by tier sweep."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.outbox import dispatch_event as outbox_dispatch
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.tiers import pin_screenshot, unpin_screenshot

router = APIRouter(tags=["pin"])


@router.post("/api/screenshots/{screenshot_id}/pin", response_class=JSONResponse)
async def pin(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        await pin_screenshot(conn, screenshot_id)
    await outbox_dispatch(
        "shot_pinned",
        {
            "shot_id": screenshot_id,
            "captured_at": shot.captured_at.isoformat(),
            "app": shot.app_name or "",
        },
    )
    return JSONResponse({"screenshot_id": screenshot_id, "tier": "pinned"})


@router.post("/api/screenshots/{screenshot_id}/unpin", response_class=JSONResponse)
async def unpin(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        await unpin_screenshot(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "tier": "hot"})


@router.post("/api/screenshots/bulk-pin", response_class=JSONResponse)
async def bulk_pin(screenshot_ids: str = Form(...)) -> JSONResponse:
    """Pin every screenshot in a comma-separated list. Capped at 500."""
    try:
        ids = [int(s) for s in screenshot_ids.split(",") if s.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid screenshot id") from exc
    if not ids:
        raise HTTPException(status_code=400, detail="No screenshots selected")
    pinned = 0
    async with get_connection() as conn:
        for sid in ids[:500]:
            await pin_screenshot(conn, sid)
            pinned += 1
    return JSONResponse({"pinned": pinned})
