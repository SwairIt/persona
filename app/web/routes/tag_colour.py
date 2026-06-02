"""v0.46 — per-tag colour customisation API.

The existing ``tags`` router exposes a numeric-id endpoint
(``POST /api/tags/{tag_id}/color``) which is fine for the in-page
colour picker that already has the row's ``tag.id`` to hand. The v0.46
work adds a *name-addressable* sibling endpoint so the chip-rendering
templates and external integrations (browser extension, future API
clients) can persist a colour without first round-tripping to look
the id up.

The route is wired into the FastAPI app from
:mod:`app.web.routes.tags` via ``router.include_router(...)`` so no
edit to :file:`app/web/main.py` is required.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.tags import set_tag_color_by_name

log = get_logger("persona.tag_colour")

# Strict 6-digit hex; matches the ``<input type="color">`` wire format.
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

router = APIRouter(tags=["tags"])


@router.post("/api/tags/{name}/color", response_class=JSONResponse)
async def set_tag_color_endpoint(
    name: str,
    color: str = Form(...),
) -> JSONResponse:
    """Persist ``color`` (CSS hex like ``#ec4899``) for tag ``name``.

    Returns ``{"name": ..., "color": ...}`` on success. Responds 400
    when the hex does not match ``^#[0-9a-fA-F]{6}$`` and 404 when the
    tag does not exist.
    """
    cleaned_name = name.strip().lower()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Empty tag name")

    cleaned_color = color.strip()
    if not _HEX_RE.match(cleaned_color):
        raise HTTPException(
            status_code=400,
            detail="Color must be a 6-digit CSS hex like #ec4899",
        )

    async with get_connection() as conn:
        try:
            tag_id = await set_tag_color_by_name(
                conn,
                cleaned_name,
                color=cleaned_color,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tag_id is None:
        log.info(
            "tag_colour.set.miss",
            tag_name=cleaned_name,
            color=cleaned_color,
        )
        raise HTTPException(status_code=404, detail=f"Tag not found: {cleaned_name}")

    log.info(
        "tag_colour.set",
        tag_id=tag_id,
        tag_name=cleaned_name,
        color=cleaned_color,
    )
    return JSONResponse({"name": cleaned_name, "color": cleaned_color, "id": tag_id})
