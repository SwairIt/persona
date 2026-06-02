"""Screenshot-of-the-day — HTML page and JSON endpoint.

The page renders a single "featured" shot that rotates once per calendar day.
See :mod:`app.shot_of_day` for the deterministic selection algorithm.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.shot_of_day import shot_of_today
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.shot_of_day")

router = APIRouter(tags=["shot_of_day"])


async def _lookup_thumbnail_path(screenshot_id: int) -> str | None:
    """Read just the ``thumbnail_path`` column for ``screenshot_id``.

    Kept out of :func:`app.shot_of_day.shot_of_today` so the public JSON payload
    stays small and storage-agnostic; the HTML page needs the on-disk path to
    construct a ``/thumbs/`` URL via the ``thumbnail_url`` Jinja filter.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT thumbnail_path FROM screenshots WHERE id = ?",
            (screenshot_id,),
        )
        row = await cursor.fetchone()
    if row is None or row["thumbnail_path"] is None:
        return None
    return str(row["thumbnail_path"])


@router.get("/shot-of-the-day", response_class=HTMLResponse)
async def shot_of_day_page(request: Request) -> HTMLResponse:
    """Render the featured screenshot for today (or an empty-state page)."""
    payload = await shot_of_today()
    thumbnail_path: str | None = None
    if payload is not None:
        thumbnail_path = await _lookup_thumbnail_path(payload["id"])
    return templates.TemplateResponse(
        request,
        "shot_of_day.html",
        {
            "title": "Shot of the day",
            "active_nav": "timeline",
            "shot": payload,
            "thumbnail_path": thumbnail_path,
        },
    )


@router.get("/api/shot-of-the-day.json", response_class=JSONResponse)
async def shot_of_day_json() -> JSONResponse:
    """Return today's featured shot as JSON, or ``404`` when there are none."""
    payload = await shot_of_today()
    if payload is None:
        raise HTTPException(status_code=404, detail="No screenshots in the last 90 days")
    return JSONResponse(dict(payload))
