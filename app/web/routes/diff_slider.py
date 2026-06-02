"""Visual before/after slider for two screenshots.

Stacks two thumbnails inside one container and lets the user drag a
vertical handle (a ``<input type="range">``) to reveal more or less of
the top image via a CSS ``clip-path`` driven by a custom property.

Distinct from :mod:`app.web.routes.analysis` (textual OCR diff) and
:mod:`app.web.routes.diff_picker` (a form to pick two IDs): this is the
*image* comparison view — same idea as GitHub's image diff slider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.time import iso as _iso
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

    from app.storage.models import Screenshot

log = get_logger("persona.diff")

router = APIRouter(tags=["analysis"])

_RANDOM_WINDOW_DAYS = 7


@router.get("/diff/random", response_class=HTMLResponse)
async def diff_slider_random(request: Request) -> HTMLResponse:
    """Render the slider with two random captures from the last 7 days.

    Demo entry point — useful for screenshots in docs and for sanity-checking
    that the slider works without first having to pick concrete IDs.
    """
    async with get_connection() as conn:
        pair = await _pick_random_pair(conn)

    if pair is None:
        log.info("diff.slider.random_empty", window_days=_RANDOM_WINDOW_DAYS)
        raise HTTPException(
            status_code=404,
            detail=f"Need at least two screenshots in the last {_RANDOM_WINDOW_DAYS} days",
        )

    shot_a, shot_b = pair
    log.info("diff.slider.random", id_a=shot_a.id, id_b=shot_b.id)
    return _render(request, shot_a, shot_b)


@router.get("/diff/{id_a}/{id_b}", response_class=HTMLResponse)
async def diff_slider_page(
    request: Request,
    id_a: int,
    id_b: int,
) -> HTMLResponse:
    """Render the before/after slider for ``id_a`` (bottom) vs ``id_b`` (top).

    Returns 404 if either screenshot is missing — keep both required so the
    page never half-renders with one broken ``<img>``.
    """
    async with get_connection() as conn:
        shot_a = await get_screenshot(conn, id_a)
        shot_b = await get_screenshot(conn, id_b)

    if shot_a is None or shot_b is None:
        missing = [i for i, s in ((id_a, shot_a), (id_b, shot_b)) if s is None]
        log.info("diff.slider.not_found", missing=missing)
        raise HTTPException(status_code=404, detail="Screenshot not found")

    log.info("diff.slider.render", id_a=id_a, id_b=id_b)
    return _render(request, shot_a, shot_b)


def _render(request: Request, shot_a: Screenshot, shot_b: Screenshot) -> HTMLResponse:
    """Shared TemplateResponse so the two endpoints stay in lockstep."""
    return templates.TemplateResponse(
        request,
        "diff_slider.html",
        {
            "title": f"Slider #{shot_a.id} vs #{shot_b.id}",
            "active_nav": "timeline",
            "shot_a": shot_a,
            "shot_b": shot_b,
        },
    )


async def _pick_random_pair(
    conn: aiosqlite.Connection,
) -> tuple[Screenshot, Screenshot] | None:
    """Pick two distinct random screenshots from the last 7 days, both with thumbs.

    Returns ``None`` if fewer than two qualifying rows exist. Ordered so the
    older one is ``shot_a`` (the "before") for a stable visual story.
    """
    since = datetime.now(tz=UTC) - timedelta(days=_RANDOM_WINDOW_DAYS)
    cursor = await conn.execute(
        "SELECT id FROM screenshots"
        " WHERE captured_at >= ? AND thumbnail_path IS NOT NULL"
        " ORDER BY RANDOM() LIMIT 2",
        (_iso(since),),
    )
    rows = list(await cursor.fetchall())
    if len(rows) < 2:
        return None

    shot_a = await get_screenshot(conn, int(rows[0]["id"]))
    shot_b = await get_screenshot(conn, int(rows[1]["id"]))
    if shot_a is None or shot_b is None:
        # Race with deletion between the two queries — treat as "not enough".
        return None
    if shot_a.captured_at > shot_b.captured_at:
        shot_a, shot_b = shot_b, shot_a
    return shot_a, shot_b
