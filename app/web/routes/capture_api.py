"""HTTP API for controlling the capture loop from the UI."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.capture import capture_primary_monitor, get_active_window
from app.dedup import compute_phash, find_or_create_dedup_group
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import (
    insert_screenshot,
    log_capture_event,
    set_dedup_group_representative,
)
from app.storage.thumbnails import save_thumbnail
from app.workers.control import get_controller

router = APIRouter(
    prefix="/api/capture",
    tags=["capture-control"],
    dependencies=[Depends(current_user_required)],
)


@router.post("/start")
async def start_capture() -> JSONResponse:
    controller = get_controller()
    controller.resume()
    async with get_connection() as conn:
        await log_capture_event(conn, "resume", {"source": "api/start"})
    return JSONResponse({"paused": controller.paused, "stopped": False})


@router.post("/pause")
async def pause_capture() -> JSONResponse:
    controller = get_controller()
    controller.pause()
    async with get_connection() as conn:
        await log_capture_event(conn, "pause", {"source": "api/pause"})
    return JSONResponse({"paused": controller.paused, "stopped": False})


@router.post("/now")
async def capture_now() -> JSONResponse:
    """Force a single capture immediately, bypassing the loop's schedule."""
    settings = get_settings()
    result = await asyncio.to_thread(capture_primary_monitor)
    window = await asyncio.to_thread(get_active_window)
    phash = compute_phash(result.image)

    async with get_connection() as conn:
        group_id, _is_new = await find_or_create_dedup_group(
            conn,
            phash=phash,
            now=result.captured_at,
            threshold=settings.dedup_hamming_threshold,
        )
        screenshot_id = await insert_screenshot(
            conn,
            captured_at=result.captured_at,
            width=result.width,
            height=result.height,
            phash=phash,
            monitor_index=result.monitor_index,
            app_name=window.app_name if window else None,
            window_title=window.title if window else None,
            process_name=window.process_name if window else None,
            ocr_status="pending" if settings.ocr_enabled else "skipped",
            dedup_group_id=group_id,
        )
        await set_dedup_group_representative(conn, group_id, screenshot_id)

    thumbnail_path = await asyncio.to_thread(
        save_thumbnail,
        result.image,
        result.captured_at,
        screenshot_id,
    )

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE screenshots SET thumbnail_path = ? WHERE id = ?",
            (str(thumbnail_path), screenshot_id),
        )
        await conn.commit()
        await log_capture_event(conn, "heartbeat", {"source": "api/now", "id": screenshot_id})

    controller = get_controller()
    controller.mark_capture()
    return JSONResponse({"screenshot_id": screenshot_id, "captured_at": result.captured_at.isoformat()})


@router.get("/status")
async def status_capture() -> JSONResponse:
    controller = get_controller()
    return JSONResponse(
        {
            "paused": controller.paused,
            "stopped": controller.stop_event.is_set(),
            "captures_total": controller.captures_total,
            "captures_skipped_dedup": controller.captures_skipped_dedup,
            "captures_skipped_idle": controller.captures_skipped_idle,
            "captures_failed": controller.captures_failed,
            "last_capture_at": (
                controller.last_capture_at.isoformat()
                if controller.last_capture_at
                else None
            ),
            "last_error_message": controller.last_error_message,
        }
    )
