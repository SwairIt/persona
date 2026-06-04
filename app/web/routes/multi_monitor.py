"""HTML + JSON routes for the multi-monitor stacked thumbnail view.

Pairs with :mod:`app.multi_monitor_view`. When ``settings.multi_monitor``
is ``True`` the capture worker persists each connected monitor as its
own ``screenshots`` row (one row per monitor, all sharing ``captured_at``
— see :func:`app.capture.screen.capture_all_monitors`). The timeline
grid only renders the first monitor's thumbnail per timestamp, so the
secondary monitor thumbnails sit on disk unused.

These routes give the operator a way to see *every* monitor for a
given capture row, stacked vertically with an index badge per tile:

* ``GET /shot/{shot_id}/monitors`` — full HTML page extending
  :file:`base.html`.
* ``GET /api/shot/{shot_id}/monitors.json`` — machine-readable list
  used by the same page (and future Alpine widgets) without paying the
  Jinja render cost.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` (task spec forbids touching ``main.py``). Wire
it up in a follow-up patch with::

    from app.web.routes import multi_monitor as multi_monitor_routes
    app.include_router(multi_monitor_routes.router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.multi_monitor_view import list_monitor_screenshots, list_monitor_thumbnails
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from app.storage.models import Screenshot

router = APIRouter(tags=["multi-monitor"])
log = get_logger("persona.multi_monitor_view")


class MonitorTile(TypedDict):
    """One row in the stacked-monitor view: thumbnail + geometry + label."""

    monitor_index: int
    url: str | None
    width: int
    height: int
    shot_id: int


def _tiles_from_siblings(siblings: list[Screenshot]) -> list[MonitorTile]:
    """Convert sibling screenshot rows into render-ready tile dicts.

    The URL field is ``None`` when a sibling row has no thumbnail on
    disk (smart-min-gap suppression at capture time, or an evicted
    file). The template renders a placeholder in that case instead of
    a broken ``<img>``.
    """
    return [
        MonitorTile(
            monitor_index=row.monitor_index,
            url=thumbnail_url(row.thumbnail_path),
            width=row.width,
            height=row.height,
            shot_id=row.id,
        )
        for row in siblings
    ]


def _single_thumb_tile(row: Screenshot) -> list[MonitorTile]:
    """Build a one-tile list from the requested row only.

    Used as the fallback when DB sibling lookup returns just the
    requested row itself (single-monitor capture, or the multi-monitor
    setting was off when this row was written). Keeps the response
    shape identical so the template renders uniformly.
    """
    return [
        MonitorTile(
            monitor_index=row.monitor_index,
            url=thumbnail_url(row.thumbnail_path),
            width=row.width,
            height=row.height,
            shot_id=row.id,
        )
    ]


async def _collect_tiles(shot_id: int) -> tuple[Screenshot, list[MonitorTile]]:
    """Return ``(requested_row, tiles)`` for the stacked view.

    Strategy:

    1. Look up the requested ``screenshots`` row. Raise ``404`` when
       missing — the operator hit a stale URL.
    2. Ask :func:`list_monitor_screenshots` for every sibling row at
       the same ``captured_at``. In the common multi-monitor case this
       returns one row per connected monitor.
    3. If the sibling count is 1, also poke
       :func:`list_monitor_thumbnails` for filename-suffix siblings
       (forward-compat ``<id>_mon<N>.webp`` convention) and synthesise
       extra tiles from any disk-only matches. When neither path finds
       anything beyond the requested row, fall back to a single-tile
       list so the template still renders.
    """
    async with get_connection() as conn:
        row = await get_screenshot(conn, shot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    siblings = await list_monitor_screenshots(shot_id)
    if len(siblings) > 1:
        return row, _tiles_from_siblings(siblings)

    # Single-row case: try the forward-compat suffix walk. If it finds
    # more than the row's own thumbnail, synthesise tiles from disk
    # alone (we have no DB row for those, so width/height fall back to
    # the requested row's geometry — acceptable for the rare case where
    # the writer eventually adopts the suffix convention but the DB
    # still records one row per physical capture).
    disk_paths = await list_monitor_thumbnails(shot_id)
    if len(disk_paths) <= 1:
        return row, _single_thumb_tile(row)

    tiles: list[MonitorTile] = []
    for idx, path in enumerate(disk_paths):
        tiles.append(
            MonitorTile(
                monitor_index=idx,
                url=thumbnail_url(str(path)),
                width=row.width,
                height=row.height,
                shot_id=row.id,
            )
        )
    return row, tiles


@router.get("/shot/{shot_id}/monitors", response_class=HTMLResponse)
async def multi_monitor_page(request: Request, shot_id: int) -> HTMLResponse:
    """Render the vertical stack of every monitor thumbnail for ``shot_id``."""
    row, tiles = await _collect_tiles(shot_id)
    log.info(
        "multi_monitor_view.page",
        shot_id=shot_id,
        tile_count=len(tiles),
    )
    return templates.TemplateResponse(
        request,
        "multi_monitor.html",
        {
            "title": "Все мониторы",  # noqa: RUF001 — Cyrillic title is intentional UI copy
            "active_nav": "timeline",
            "shot": row,
            "tiles": tiles,
        },
    )


@router.get("/api/shot/{shot_id}/monitors.json")
async def multi_monitor_json(shot_id: int) -> JSONResponse:
    """Return the per-monitor tile list as JSON.

    Schema: ``[{monitor_index, url, width, height, shot_id}, ...]`` —
    same shape the HTML page consumes, so a future Alpine widget can
    re-render without a round-trip to the templated page.
    """
    _, tiles = await _collect_tiles(shot_id)
    log.info(
        "multi_monitor_view.json",
        shot_id=shot_id,
        tile_count=len(tiles),
    )
    payload: list[dict[str, object]] = [
        {
            "monitor_index": tile["monitor_index"],
            "url": tile["url"],
            "width": tile["width"],
            "height": tile["height"],
            "shot_id": tile["shot_id"],
        }
        for tile in tiles
    ]
    return JSONResponse(payload)
