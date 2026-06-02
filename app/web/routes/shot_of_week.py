"""Screenshot-of-the-week — HTML page and JSON endpoint.

The page renders a single "featured" shot picked by user-signal scoring
(pinned / favourited / tag count / annotation count) over the current ISO
week. See :mod:`app.shot_of_week` for the scoring algorithm and fallback
behaviour.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.shot_of_week import (
    SCORE_FAVOURITED,
    SCORE_PER_ANNOTATION,
    SCORE_PER_TAG,
    SCORE_PINNED,
    shot_of_this_week,
)
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.shot_of_week")

router = APIRouter(tags=["shot_of_week"])


async def _lookup_thumbnail_path(screenshot_id: int) -> str | None:
    """Read just the ``thumbnail_path`` column for ``screenshot_id``.

    Kept out of :func:`app.shot_of_week.shot_of_this_week` so the public JSON
    payload stays small and storage-agnostic; the HTML page needs the on-disk
    path to construct a ``/thumbs/`` URL via the ``thumbnail_url`` filter.

    Parametrised SQL — never string-interpolate the id even though it's an
    int locally, because the column is filtered by an integer comparison and
    SQLite caches the prepared statement.
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


@router.get("/shot-of-the-week", response_class=HTMLResponse)
async def shot_of_week_page(request: Request) -> HTMLResponse:
    """Render the curated weekly highlight (or an empty-state page)."""
    payload = await shot_of_this_week()
    thumbnail_path: str | None = None
    if payload is not None:
        thumbnail_path = await _lookup_thumbnail_path(payload["id"])
    # Surface the score weights to the template so the breakdown labels stay
    # in sync with the algorithm — change the constant in one place, both
    # the score math and the UI ("Pinned ✓ +5") update together.
    return templates.TemplateResponse(
        request,
        "shot_of_week.html",
        {
            "title": "Shot of the week",
            "active_nav": "timeline",
            "shot": payload,
            "thumbnail_path": thumbnail_path,
            "score_pinned": SCORE_PINNED,
            "score_favourited": SCORE_FAVOURITED,
            "score_per_tag": SCORE_PER_TAG,
            "score_per_annotation": SCORE_PER_ANNOTATION,
        },
    )


@router.get("/api/shot-of-the-week.json", response_class=JSONResponse)
async def shot_of_week_json() -> JSONResponse:
    """Return this week's featured shot as JSON, or ``404`` if there are no
    screenshots anywhere (week query empty *and* daily fallback empty)."""
    payload = await shot_of_this_week()
    if payload is None:
        raise HTTPException(status_code=404, detail="No screenshots available")
    return JSONResponse(dict(payload))
