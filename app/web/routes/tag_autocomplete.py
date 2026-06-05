"""HTTP surface for the tag autocomplete suggester.

Three endpoints, all read-only:

* ``GET /api/tags/autocomplete?prefix=...&limit=...`` — JSON list of
  ``{"tag", "count", "source"}`` rows whose names start with ``prefix``.
* ``GET /api/tags/all`` — JSON dump of every known tag with its merged count,
  capped at 500 rows. Useful for cache-priming the palette or seeding a
  client-side fuzzy index.
* ``GET /widget/tag-autocomplete?prefix=...`` — HTML fragment with a
  ``<datalist>`` plus a button-list fallback, HTMX-embeddable.

This module deliberately does NOT register itself with the FastAPI app in
:mod:`app.web.main` — the task spec forbids touching ``main.py``. Wire it up
with::

    from app.web.routes import tag_autocomplete as tag_autocomplete_routes
    app.include_router(tag_autocomplete_routes.router)
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.tag_autocomplete import all_tags, suggest_tags
from app.web.templates_engine import templates

log = get_logger("persona.tag_autocomplete.routes")

router = APIRouter(tags=["tag-autocomplete"])


# Hard ceiling on the ``limit`` query parameter. Mirrors ``_MAX_LIMIT`` inside
# :mod:`app.tag_autocomplete` — kept duplicated here so the FastAPI ``Query``
# constraint surfaces a friendly 422 before we hit the suggester instead of
# silently clamping inside the function.
_AUTOCOMPLETE_MAX_LIMIT: Final[int] = 100

# Default ``limit`` for the autocomplete endpoint when the caller omits it.
# Matches the suggester default so the documented behaviour is consistent
# across the JSON and the HTML surfaces.
_AUTOCOMPLETE_DEFAULT_LIMIT: Final[int] = 10

# Cap on the ``/api/tags/all`` response. The spec pins this at 500 rows —
# enough to seed a client-side fuzzy index without bloating the payload past
# a few KB of gzipped JSON.
_ALL_TAGS_CAP: Final[int] = 500

# Cap on the ``prefix`` query parameter. Matches the in-module clamp inside
# :func:`app.tag_autocomplete.suggest_tags`, again duplicated here so the 422
# fires at the edge rather than relying on silent truncation.
_PREFIX_MAX_LENGTH: Final[int] = 64


@router.get(
    "/api/tags/autocomplete",
    response_class=JSONResponse,
)
async def tag_autocomplete_json(
    prefix: str = Query(
        default="",
        max_length=_PREFIX_MAX_LENGTH,
        description="Case-insensitive prefix; empty matches all tags.",
    ),
    limit: int = Query(
        default=_AUTOCOMPLETE_DEFAULT_LIMIT,
        ge=1,
        le=_AUTOCOMPLETE_MAX_LIMIT,
        description="Maximum number of suggestions to return.",
    ),
) -> JSONResponse:
    """Return prefix-matched tag suggestions as JSON.

    Empty ``prefix`` is allowed and returns the top-``limit`` tags overall —
    handy for the palette's "show recent tags" surface. The response is a
    JSON array of objects with ``tag`` / ``count`` / ``source`` keys; the
    array is empty (not 404) when nothing matches so the client can render
    "no matches" inline without a special status code path.
    """
    suggestions = await suggest_tags(prefix, limit=limit)
    return JSONResponse([dict(item) for item in suggestions])


@router.get(
    "/api/tags/all",
    response_class=JSONResponse,
)
async def tag_autocomplete_all(
    limit: int = Query(
        default=_ALL_TAGS_CAP,
        ge=1,
        le=_ALL_TAGS_CAP,
        description="Hard cap on the returned list (max 500).",
    ),
) -> JSONResponse:
    """Return every known tag with its merged count, capped at 500 rows.

    The optional ``limit`` query parameter lets callers ask for a smaller
    slice (the autocomplete dropdown's "recent tags" hint, for example) but
    can never exceed the documented 500-row ceiling — anything larger is
    rejected with a 422 by FastAPI's ``Query`` constraint.
    """
    suggestions = await all_tags(limit=limit)
    return JSONResponse([dict(item) for item in suggestions])


@router.get(
    "/widget/tag-autocomplete",
    response_class=HTMLResponse,
)
async def tag_autocomplete_widget(
    request: Request,
    prefix: str = Query(
        default="",
        max_length=_PREFIX_MAX_LENGTH,
        description="Case-insensitive prefix; empty matches all tags.",
    ),
    limit: int = Query(
        default=_AUTOCOMPLETE_DEFAULT_LIMIT,
        ge=1,
        le=_AUTOCOMPLETE_MAX_LIMIT,
        description="Maximum number of suggestions to render.",
    ),
) -> HTMLResponse:
    """Render the autocomplete fragment for HTMX embedding.

    Returns the small standalone fragment defined in
    :file:`_tag_autocomplete.html`. The fragment exposes both a
    ``<datalist>`` (so a native ``<input list>`` can wire up to it for free)
    and a button list (for HTMX click-to-fill flows where ``<datalist>`` is
    too coarse — keyboard-only users, mobile webviews that hide ``<datalist>``
    UI, etc.).
    """
    suggestions = await suggest_tags(prefix, limit=limit)
    if not isinstance(prefix, str):  # pragma: no cover — FastAPI guarantees str
        raise HTTPException(status_code=400, detail="prefix must be a string")
    return templates.TemplateResponse(
        request,
        "_tag_autocomplete.html",
        {
            "prefix": prefix,
            "suggestions": suggestions,
        },
    )


__all__ = ["router"]
