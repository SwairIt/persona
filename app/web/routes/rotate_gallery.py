"""Fullscreen auto-rotating carousel for a tag or FTS query.

Two endpoints:

* ``GET /gallery?q=QUERY&interval=5`` — renders a minimalist fullscreen
  page that cycles through the matching screenshots every ``interval``
  seconds. ``QUERY`` is either ``#tagname`` (resolved against the tag
  storage) or any free-text fragment (run through the FTS5 pipeline).
* ``GET /api/gallery.json?q=QUERY`` — returns the same ordered list of
  screenshot ids as JSON, so the client-side carousel can refresh the
  ordering without a full page reload.

The route deliberately does not extend ``base.html`` — a slideshow on a
projector / second monitor wants every pixel for the image, not chrome.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.search import search as run_fts_search
from app.storage.db import get_connection
from app.storage.tags import list_screenshots_by_tag, list_tags
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

log = get_logger("persona.gallery")

router = APIRouter(tags=["gallery"])

# Carousel cadence guardrails. Sub-second rotation strobes; >10 minutes
# is effectively "no rotation" so we let the user opt out by passing a
# huge value but clamp the practical UI range here.
_MIN_INTERVAL_SECONDS = 1
_MAX_INTERVAL_SECONDS = 600
_DEFAULT_INTERVAL_SECONDS = 5

# Hard cap on the playlist length so the JSON payload + the initial HTML
# preload stay bounded. The carousel is designed for "show me this tag"
# moments, not "play every screenshot I've ever taken".
_MAX_PLAYLIST = 500


def _coerce_interval(value: int | None) -> int:
    """Clamp ``interval`` query param to a sane projector-friendly range."""
    if value is None:
        return _DEFAULT_INTERVAL_SECONDS
    if value < _MIN_INTERVAL_SECONDS:
        return _MIN_INTERVAL_SECONDS
    if value > _MAX_INTERVAL_SECONDS:
        return _MAX_INTERVAL_SECONDS
    return value


async def _resolve_playlist(query: str) -> list[dict[str, Any]]:
    """Turn a ``q`` string into an ordered list of ``{id, thumbnail_path}``.

    Two query shapes are supported:

    * ``#tagname`` — exact (case-insensitive) tag match. The leading
      ``#`` is stripped before lookup; an unknown tag yields an empty
      playlist (the page renders a friendly empty state).
    * anything else — handed to :func:`app.search.search` as an FTS5
      query. Bare words get the ``*`` prefix-match treatment for free.

    Results are deduplicated while preserving order (the FTS path may
    return the same shot more than once when the underlying ``MATCH``
    expression hits multiple OCR rows) and capped at
    :data:`_MAX_PLAYLIST` so the embedded JSON stays under the size we
    care to ship to the browser.
    """
    cleaned = query.strip()
    if not cleaned:
        return []

    async with get_connection() as conn:
        if cleaned.startswith("#"):
            tag_name = cleaned[1:].strip().lower()
            if not tag_name:
                return []
            all_tags = await list_tags(conn)
            match = next((t for t in all_tags if str(t["name"]).lower() == tag_name), None)
            if match is None:
                log.info("gallery.tag_miss", tag=tag_name)
                return []
            shot_ids = await list_screenshots_by_tag(
                conn, int(match["id"]), limit=_MAX_PLAYLIST,
            )
            if not shot_ids:
                return []
            placeholders = ",".join("?" * len(shot_ids))
            cursor = await conn.execute(
                "SELECT id, thumbnail_path, captured_at "  # noqa: S608 — placeholders only
                f"FROM screenshots WHERE id IN ({placeholders}) "
                "ORDER BY captured_at DESC",
                tuple(shot_ids),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "id": int(row["id"]),
                    "thumbnail_path": row["thumbnail_path"],
                    "captured_at": row["captured_at"],
                }
                for row in rows
            ]

        hits = await run_fts_search(conn, query=cleaned, limit=_MAX_PLAYLIST)

    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for hit in hits:
        sid = int(hit.screenshot_id)
        if sid in seen:
            continue
        seen.add(sid)
        out.append(
            {
                "id": sid,
                "thumbnail_path": hit.thumbnail_path,
                "captured_at": hit.captured_at.isoformat() if hit.captured_at else None,
            }
        )
    return out


@router.get("/gallery", response_class=HTMLResponse)
async def gallery_page(
    request: Request,
    q: str = Query(default=""),
    interval: int | None = Query(default=None, ge=0),
) -> HTMLResponse:
    """Render the fullscreen auto-rotating carousel for ``q``."""
    chosen_interval = _coerce_interval(interval)
    playlist = await _resolve_playlist(q)
    # Serialise the playlist as JSON for the inline ``<script>`` block:
    # the client carousel only needs id + image URL, so we strip the
    # heavier metadata before shipping. We also escape ``<`` / ``>`` so
    # a malicious ``thumbnail_path`` containing ``</script>`` cannot
    # break out of the JSON-embedded script tag in the template.
    payload = [
        {"id": item["id"], "url": thumbnail_url(item["thumbnail_path"]) or ""}
        for item in playlist
        if item["thumbnail_path"]
    ]
    playlist_json = (
        json.dumps(payload).replace("<", "\\u003c").replace(">", "\\u003e")
    )
    log.info(
        "gallery.page_render",
        query=q,
        interval=chosen_interval,
        count=len(payload),
    )
    return templates.TemplateResponse(
        request,
        "rotate_gallery.html",
        {
            "title": f"Gallery: {q}" if q else "Gallery",
            "query": q,
            "interval": chosen_interval,
            "playlist_json": playlist_json,
            "total": len(payload),
        },
    )


@router.get("/api/gallery.json", response_class=JSONResponse)
async def gallery_json(q: str = Query(default="")) -> JSONResponse:
    """Return the ordered list of shot ids for ``q`` as JSON."""
    playlist = await _resolve_playlist(q)
    log.info("gallery.json_served", query=q, count=len(playlist))
    return JSONResponse(
        {
            "query": q,
            "total": len(playlist),
            "ids": [item["id"] for item in playlist],
        }
    )
