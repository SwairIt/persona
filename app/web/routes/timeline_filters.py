"""HTTP surface for the timeline filter-chip bar.

Three endpoints — same chip catalogue, three render modes:

* ``GET /timeline/filters``
      Full standalone page (extends ``base.html``). Demo / help screen
      that previews the chip pill row and explains every chip's SQL
      effect so a user can discover what the bar does without trial and
      error.
* ``GET /api/timeline/filters/state.json?chips=pinned_only,today``
      Machine-readable mirror of :func:`build_chip_state`. Useful for
      the future mobile UI / external dashboards that want to render
      the same chips without re-deriving SQL.
* ``GET /widget/timeline-filter-chips``
      Standalone HTML fragment (no ``base.html``) that contains *just*
      the chip row. The timeline template pulls this in via
      ``hx-get="/widget/timeline-filter-chips" hx-trigger="load"`` so
      one HTMX swap can repaint the bar without rerunning the page
      chrome.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.timeline_filters import TIMELINE_FILTERS, build_chip_state
from app.web.templates_engine import templates

log = get_logger("persona.timeline_filters.routes")

router = APIRouter(tags=["timeline-filters"])


def _parse_chips_csv(value: str | None) -> list[str]:
    """Split ``?chips=a,b,c`` into a clean list of non-empty ids.

    Empty inputs, repeated commas, and surrounding whitespace are
    swallowed silently — :func:`build_chip_state` drops unknown ids
    anyway, so we only need to keep the boundary loose.
    """
    if not value:
        return []
    return [piece.strip() for piece in value.split(",") if piece.strip()]


@router.get("/timeline/filters", response_class=HTMLResponse)
async def timeline_filters_page(
    request: Request,
    chips: str | None = Query(default=None),
    app: str | None = Query(default=None),
    window: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the standalone help / preview page for the chip bar.

    The page extends ``base.html`` (so it gets the full chrome /
    navigation) and embeds the same partial used by the HTMX widget so
    the live preview and the production bar can never diverge.
    """
    state = await build_chip_state(_parse_chips_csv(chips), app=app, window=window)

    return templates.TemplateResponse(
        request,
        "timeline_filters.html",
        {
            "title": "Timeline filter chips",
            "active_nav": "timeline",
            "state": state,
            "catalogue": TIMELINE_FILTERS,
            "current_app": app,
            "current_window": window,
        },
    )


@router.get("/api/timeline/filters/state.json", response_class=JSONResponse)
async def timeline_filters_state_json(
    chips: str | None = Query(default=None),
    app: str | None = Query(default=None),
    window: str | None = Query(default=None),
) -> JSONResponse:
    """Return :func:`build_chip_state` as JSON for non-HTML callers.

    The shape mirrors :class:`app.timeline_filters.ChipStateResult` 1:1.
    """
    state = await build_chip_state(_parse_chips_csv(chips), app=app, window=window)
    # ``ChipStateResult`` is a ``TypedDict`` (plain ``dict`` at runtime),
    # so we can hand it straight to :class:`JSONResponse`.
    return JSONResponse(dict(state))


@router.get("/widget/timeline-filter-chips", response_class=HTMLResponse)
async def timeline_filter_chips_widget(
    request: Request,
    chips: str | None = Query(default=None),
    app: str | None = Query(default=None),
    window: str | None = Query(default=None),
) -> HTMLResponse:
    """Return the chip row as a standalone HTML fragment.

    No ``base.html`` extension — this is meant to be pulled in via
    ``hx-get`` from the timeline page. Returning a fragment means a
    second HTMX swap (e.g. after the user toggles a chip) can repaint
    the bar in place without reloading the whole page.
    """
    state = await build_chip_state(_parse_chips_csv(chips), app=app, window=window)

    return templates.TemplateResponse(
        request,
        "_timeline_filter_chips.html",
        {
            "state": state,
            "current_app": app,
            "current_window": window,
        },
    )
