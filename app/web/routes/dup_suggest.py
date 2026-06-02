"""HTTP route for "Possibly related" screenshot suggestions.

Persona v0.47 feature 2/3. Powers the strip at the bottom of the
screenshot detail page — HTMX hits this endpoint on page load and
swaps the response into a small grid of up to four thumbnails.

Endpoint
--------
* ``GET /api/screenshot/{shot_id}/similar.json`` — returns an HTML
  fragment (a ``<div>`` containing the suggestion cards, or a single
  comment node when there is nothing to show). The path keeps the
  ``.json`` suffix from the spec for predictable routing even though
  the body is HTML — HTMX cares about the URL, not the content type.

We return HTML rather than JSON so the existing HTMX call site can
just ``hx-swap`` the fragment in. The shape of each suggestion is
still the documented ``{id, captured_at, app_name, thumbnail_url,
reason}`` dict — we hand it through :func:`app.dup_suggest.suggest_similar`
and Jinja's ``tojson`` filter inside the template would have to escape
it anyway, so doing the render here avoids a second round trip.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import HTMLResponse

from app.dup_suggest import suggest_similar
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.dup_suggest")

router = APIRouter(tags=["dup-suggest"])

# Hard cap on ``limit`` — the endpoint is HTMX-driven so the strip
# only ever needs four cards, but somebody might hand-craft a URL.
# We accept up to ten so a future settings toggle can widen it
# without a route change, but never more than that.
_MAX_LIMIT = 10
_DEFAULT_LIMIT = 4


@router.get(
    "/api/screenshot/{shot_id}/similar.json",
    response_class=HTMLResponse,
)
async def screenshot_similar(
    request: Request,
    shot_id: Annotated[int, Path(ge=1)],
    limit: int = _DEFAULT_LIMIT,
) -> HTMLResponse:
    """Render the "Possibly related" strip for one screenshot.

    Returns an HTML fragment that HTMX swaps directly into the detail
    page. When there are no suggestions (no group, no near-pHash
    neighbours, or a missing seed row), the fragment is an empty
    container — the strip silently disappears rather than showing
    a misleading "no results" banner.
    """
    safe_limit = max(1, min(int(limit), _MAX_LIMIT))
    suggestions = await suggest_similar(shot_id, limit=safe_limit)

    log.info(
        "dup_suggest.served",
        shot_id=shot_id,
        limit=safe_limit,
        returned=len(suggestions),
    )

    return templates.TemplateResponse(
        request,
        "_dup_suggest_strip.html",
        {
            "shot_id": shot_id,
            "suggestions": suggestions,
        },
    )
