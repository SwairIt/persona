"""HTTP surface for the per-app summary card.

Three endpoints, all keyed by ``app_name`` (URL-encoded segment, FastAPI
decodes for us — see the existing :mod:`app.web.routes.app_icons`
module for the same pattern):

* ``GET /app/{app_name}/summary``      — standalone Tailwind page that
  extends ``base.html`` and renders the single-card view.
* ``GET /api/app/{app_name}/summary.json`` — JSON view of the same dict
  for future widget shells / CLI consumers.
* ``GET /widget/app-card/{app_name}``   — bare HTML fragment for HTMX
  embedding. No outer chrome, no ``base.html`` — just the card markup
  so a parent page can drop it into a column without inheriting the
  Persona navbar twice.

This module does **not** register itself with the FastAPI app — the
task spec forbids touching ``app/web/main.py``. Wiring is one line in
the include_router section there:

    from app.web.routes import app_summary_card as app_summary_card_routes
    app.include_router(app_summary_card_routes.router)

The data layer (``build_app_card``) lives in
:mod:`app.app_summary_card`; this module is the thin HTTP wrapper that
runs it and dispatches to one of three response shapes.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import unquote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.app_icon_chips import ensure_icon_for
from app.app_summary_card import build_app_card, days_since
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.app_summary_card.web")

router = APIRouter(tags=["app-summary-card"])

# Cap on the path-encoded ``app_name``. SQLite would accept anything,
# but a 4 KB segment is almost certainly a probe — we mirror the
# defensive ceiling already in :mod:`app.web.routes.app_icons`.
_MAX_APP_NAME_LEN: Final[int] = 512

# Floor / ceiling on the optional ``?days=N`` query parameter. Matches
# the bounds enforced inside :func:`app.app_summary_card.build_app_card`
# so a 422 here means "you typoed it, not we silently clamped".
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365


def _decode_app_name(raw: str) -> str:
    """Normalise the path segment into the canonical ``app_name`` string.

    FastAPI already runs ``unquote`` once for path params; a defensive
    second pass catches the double-encoded form (``%2520``) that
    bookmarklets and a few legacy CLI clients still produce. Trimmed
    and length-capped so a pathological caller can't push the SQL
    parameter past a sane bound.
    """
    decoded = unquote(raw or "").strip()
    return decoded[:_MAX_APP_NAME_LEN]


@router.get("/app/{app_name}/summary", response_class=HTMLResponse)
async def app_summary_card_page(
    request: Request,
    app_name: str,
    days: int = Query(default=7, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the single-card page for ``app_name``.

    Uses ``app_summary_card.html`` (extends ``base.html``, title
    ``App: {app_name}``, ``active_nav`` ``stats`` to highlight the
    Stats nav slot — matches the same scheme used by
    ``/apps/{name}`` on :mod:`app.web.routes.app_stats`).
    """
    name = _decode_app_name(app_name)
    card = await build_app_card(name, days=days)
    icon = await ensure_icon_for(name)
    log.info(
        "app_summary_card.page",
        app_name=name,
        days=card["days"],
        total_shots=card["total_shots"],
    )
    return templates.TemplateResponse(
        request,
        "app_summary_card.html",
        {
            "title": f"App: {name}",
            "active_nav": "stats",
            "app_name": name,
            "card": card,
            "icon": icon,
            "first_seen_days_ago": days_since(card["first_seen"]),
            "last_seen_days_ago": days_since(card["last_seen"]),
        },
    )


@router.get("/api/app/{app_name}/summary.json")
async def app_summary_card_json(
    app_name: str,
    days: int = Query(default=7, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw card dict as JSON.

    Exposes exactly what :func:`build_app_card` returns plus the two
    derived ``*_days_ago`` integers that the HTML template uses — so a
    downstream widget shell can pick either the raw timestamps or the
    pre-computed deltas without re-implementing the math.
    """
    name = _decode_app_name(app_name)
    card = await build_app_card(name, days=days)
    payload = dict(card)
    payload["first_seen_days_ago"] = days_since(card["first_seen"])
    payload["last_seen_days_ago"] = days_since(card["last_seen"])
    log.info("app_summary_card.json", app_name=name, days=card["days"])
    return JSONResponse(content=payload)


@router.get("/widget/app-card/{app_name}", response_class=HTMLResponse)
async def app_summary_card_widget(
    request: Request,
    app_name: str,
    days: int = Query(default=7, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the standalone HTML fragment for HTMX consumers.

    Body is a single ``<article>`` with the card markup — no ``<html>``
    wrapper, no Persona navbar — so a parent page can drop it into a
    column with ``hx-get="/widget/app-card/Chrome"`` and not inherit
    duplicate chrome. Internally this just renders the same partial
    the full page composes from, so the two views stay in lockstep.
    """
    name = _decode_app_name(app_name)
    card = await build_app_card(name, days=days)
    icon = await ensure_icon_for(name)
    log.info(
        "app_summary_card.widget",
        app_name=name,
        days=card["days"],
        total_shots=card["total_shots"],
    )
    return templates.TemplateResponse(
        request,
        "_app_summary_card_fragment.html",
        {
            "app_name": name,
            "card": card,
            "icon": icon,
            "first_seen_days_ago": days_since(card["first_seen"]),
            "last_seen_days_ago": days_since(card["last_seen"]),
        },
    )


__all__ = ["router"]
