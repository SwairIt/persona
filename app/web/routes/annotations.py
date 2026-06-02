"""HTTP routes for per-screenshot annotations.

Endpoints:

* ``GET  /api/screenshot/{shot_id}/annotations`` — list as JSON;
* ``POST /api/screenshot/{shot_id}/annotations`` — append (form field ``body``);
* ``POST /api/annotation/{ann_id}/delete``      — delete by id.

Notes vs annotations vs tags is a deliberate three-way split — see
``app/storage/annotations.py`` for the rationale.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.annotations import add, delete, list_for_screenshot
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

log = get_logger("persona.annotations")

router = APIRouter(tags=["annotations"])


@router.get("/api/screenshot/{shot_id}/annotations", response_class=JSONResponse)
async def list_annotations(shot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        items: list[dict[str, Any]] = await list_for_screenshot(conn, shot_id)
    return JSONResponse(items)


@router.post("/api/screenshot/{shot_id}/annotations", response_class=JSONResponse)
async def create_annotation(
    shot_id: int,
    body: str = Form(...),
) -> JSONResponse:
    text = (body or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="body must not be empty")

    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        created = await add(conn, shot_id, text)

    return JSONResponse(created, status_code=201)


@router.post("/api/annotation/{ann_id}/delete", response_class=JSONResponse)
async def delete_annotation(ann_id: int) -> JSONResponse:
    async with get_connection() as conn:
        removed = await delete(conn, ann_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Annotation not found")
    return JSONResponse({"id": ann_id, "deleted": True})
