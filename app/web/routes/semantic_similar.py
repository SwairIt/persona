"""HTTP route for the "Semantically related" screenshot strip.

Persona v0.68 feature 1/3. Mirrors the v0.47 ``dup_suggest`` route —
HTMX hits this endpoint on screenshot-detail page load and swaps the
response into a strip below the existing "Possibly related" cards.

Endpoint
--------
* ``GET /api/screenshot/{shot_id}/semantic-similar.json`` — returns an
  HTML fragment (a ``<section>`` containing the suggestion cards, or a
  hidden empty section). The ``.json`` suffix is kept from the spec
  for predictable routing even though the body is HTML; HTMX cares
  about the URL, not the content type. Same convention the v0.47
  ``similar.json`` sibling already uses.

We render HTML rather than JSON so the existing HTMX call site can
straight ``hx-swap`` the fragment in without an extra Alpine wrapper.
The underlying :func:`app.semantic_similar.similar_to` still returns
plain dicts, so any future JSON consumer can call the helper directly
without going through the template.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.semantic_similar import similar_to
from app.web.templates_engine import templates

log = get_logger("persona.semantic_similar")

router = APIRouter(tags=["semantic-similar"])

# Hard cap on ``limit``. The detail-page strip uses six cards by
# default — we accept up to twelve so a future settings toggle can
# widen the strip without a route change, but never more than that.
# A request that bypasses the UI and asks for 9999 results would
# otherwise burn CPU on a brute-force cosine scan with no UI surface.
_MAX_LIMIT = 12
_DEFAULT_LIMIT = 6


@router.get(
    "/api/screenshot/{shot_id}/semantic-similar.json",
    response_class=HTMLResponse,
)
async def screenshot_semantic_similar(
    request: Request,
    shot_id: Annotated[int, Path(ge=1)],
    limit: int = _DEFAULT_LIMIT,
) -> HTMLResponse:
    """Render the "Semantically related" strip for one screenshot.

    Returns an HTML fragment HTMX swaps directly into the detail page.
    When the helper returns an empty list (no seed embedding, numpy
    missing, nothing above the similarity floor), the fragment is a
    hidden section so the page layout does not jump.
    """
    safe_limit = max(1, min(int(limit), _MAX_LIMIT))
    suggestions = await similar_to(shot_id, limit=safe_limit)

    log.info(
        "semantic_similar.served",
        shot_id=shot_id,
        limit=safe_limit,
        returned=len(suggestions),
    )

    return templates.TemplateResponse(
        request,
        "_semantic_similar_strip.html",
        {
            "shot_id": shot_id,
            "suggestions": suggestions,
        },
    )
