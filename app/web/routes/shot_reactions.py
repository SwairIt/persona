"""HTTP routes for per-screenshot emoji reactions.

Three endpoints:

* ``POST /api/screenshot/{shot_id}/react`` — JSON body ``{"emoji": "..."}``
  toggles the reaction and returns the
  :func:`app.shot_reactions.toggle_reaction` payload;
* ``GET /api/screenshot/{shot_id}/reactions.json`` — JSON list of every
  reaction row currently attached to the shot;
* ``GET /reactions`` — HTML page that renders the top-reacted thumbnail
  grid, optionally filtered by a single emoji via ``?emoji=``.

The HTML page extends ``base.html`` with ``title="Реакции"`` and an
``active_nav="timeline"`` highlight (reactions live inside the
screenshot-feed surface area, not under stats).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.shot_reactions import (
    ALLOWED_EMOJI,
    list_reactions_for_shot,
    toggle_reaction,
    top_reacted_shots,
)
from app.web.templates_engine import templates

log = get_logger("persona.shot_reactions.routes")

router = APIRouter(tags=["shot_reactions"])

# Cap the grid so a runaway top-N can't render thousands of cards. The
# default mirrors the spec's ``limit=20``; clients can request fewer
# via ``?limit=`` but never more.
_TOP_LIMIT_DEFAULT = 20
_TOP_LIMIT_MAX = 100


def _coerce_emoji_body(payload: object) -> str:
    """Extract ``emoji`` from a parsed JSON body or raise ``HTTPException``.

    Centralised so the POST handler stays small and we don't leak the
    raw KeyError / TypeError up to the response layer.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    emoji = payload.get("emoji")
    if not isinstance(emoji, str) or not emoji:
        raise HTTPException(
            status_code=400,
            detail="body must contain a non-empty 'emoji' string",
        )
    return emoji


@router.post(
    "/api/screenshot/{shot_id}/react",
    response_class=JSONResponse,
)
async def react_to_screenshot(shot_id: int, request: Request) -> JSONResponse:
    """Toggle a reaction emoji on a screenshot and return the new state."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    emoji = _coerce_emoji_body(body)

    try:
        result = await toggle_reaction(shot_id, emoji)
    except ValueError as exc:
        # Emoji outside the allowed vocabulary — surface as 400 instead
        # of leaking the storage-layer message verbatim.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return JSONResponse(result)


@router.get(
    "/api/screenshot/{shot_id}/reactions.json",
    response_class=JSONResponse,
)
async def reactions_json(shot_id: int) -> JSONResponse:
    """Return every reaction row attached to ``shot_id`` as JSON."""
    items = await list_reactions_for_shot(shot_id)
    return JSONResponse(items)


@router.get("/reactions", response_class=HTMLResponse)
async def reactions_page(request: Request) -> HTMLResponse:
    """Render the top-reacted grid, optionally filtered by ``?emoji=``."""
    raw_emoji = request.query_params.get("emoji")
    selected_emoji: str | None = None
    if raw_emoji:
        if raw_emoji not in ALLOWED_EMOJI:
            # Silently treat an unknown filter as "no filter" — easier
            # than 400-ing a normal user who edited the URL by hand.
            log.info(
                "shot_reactions.unknown_emoji_filter",
                requested=raw_emoji,
            )
        else:
            selected_emoji = raw_emoji

    raw_limit = request.query_params.get("limit")
    limit = _TOP_LIMIT_DEFAULT
    if raw_limit:
        try:
            limit = max(1, min(_TOP_LIMIT_MAX, int(raw_limit)))
        except ValueError:
            limit = _TOP_LIMIT_DEFAULT

    shots: list[dict[str, Any]] = await top_reacted_shots(
        emoji=selected_emoji,
        limit=limit,
    )

    return templates.TemplateResponse(
        request,
        "reactions.html",
        {
            "title": "Реакции",
            "active_nav": "timeline",
            "shots": shots,
            "allowed_emoji": ALLOWED_EMOJI,
            "selected_emoji": selected_emoji,
            "limit": limit,
        },
    )
