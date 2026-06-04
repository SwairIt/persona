"""HTTP routes for the OPML bundle of every Persona RSS feed.

``GET /feeds.opml`` serves the OPML document built by
:func:`app.opml_export.build_opml` — one-click subscribe-everything
for power users running a feed reader.

``GET /feeds/all-opml`` renders a small download page (extends
``base.html``) so the OPML bundle is discoverable from the navbar's
settings section without forcing the operator to know the magic URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.logging_setup import get_logger
from app.opml_export import build_opml
from app.web.templates_engine import templates

router = APIRouter(tags=["feeds"])

log = get_logger("persona.opml_export.route")

# Standard MIME type for OPML — RFC-free but ``text/x-opml`` is what
# every mainstream reader (Feedly, NetNewsWire, Inoreader, FreshRSS,
# Reeder) expects when importing.
_OPML_MEDIA_TYPE = "text/x-opml; charset=utf-8"
_OPML_FILENAME = "persona-feeds.opml"


def _host_from_request(request: Request) -> str:
    """Return the request origin without the trailing slash.

    FastAPI's ``request.base_url`` always carries a trailing ``/``;
    :func:`app.opml_export.build_opml` strips it too but doing so here
    keeps the structlog entry below readable.
    """
    return str(request.base_url).rstrip("/")


@router.get("/feeds.opml", response_model=None)
async def feeds_opml(
    request: Request,
    token: str | None = Query(default=None),
) -> Response:
    """Serve the OPML 2.0 bundle of every canonical Persona feed.

    The ``token`` query param is optional. When supplied it's woven
    into every feed URL inside the bundle as ``?token=…`` so a single
    import covers token-gated subscriptions — no per-feed re-entry of
    the token in the reader.
    """
    host = _host_from_request(request)
    body = build_opml(host=host, token=token)
    payload = body.encode("utf-8")

    log.info(
        "opml.route.served",
        host=host,
        token_present=bool(token),
        bytes=len(payload),
    )

    return Response(
        content=payload,
        media_type=_OPML_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{_OPML_FILENAME}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


@router.get("/feeds/all-opml", response_class=HTMLResponse)
async def feeds_all_opml_page(request: Request) -> HTMLResponse:
    """Render the discoverable download page for the OPML bundle.

    Read-only: no DB access — just a Jinja render so operators can find
    the OPML download without memorising ``/feeds.opml``.
    """
    log.info("opml.page.rendered")
    return templates.TemplateResponse(
        request,
        "feeds_all_opml.html",
        {
            "title": "Все RSS-ленты OPML",  # noqa: RUF001 — Cyrillic page title
            "active_nav": "settings",
        },
    )


__all__ = ["router"]
