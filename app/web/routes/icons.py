"""Serve cached app icons. Tries to extract on demand if missing."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from app.capture import ensure_icon_cached, icon_path_for

router = APIRouter(tags=["icons"])


@router.get("/icons/{process_name}.png")
async def get_icon(process_name: str) -> Response:
    path = icon_path_for(process_name)
    if path is None:
        raise HTTPException(status_code=404, detail="Bad process name")
    if not path.exists():
        # Try to extract on demand (Windows only, no-op elsewhere)
        await asyncio.to_thread(ensure_icon_cached, process_name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No cached icon for this app")
    return FileResponse(path, media_type="image/png")
