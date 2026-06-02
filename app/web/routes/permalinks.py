"""HTTP surface for shareable permalinks.

Three endpoints live here:

* ``POST /api/permalink`` — form-encoded ``target_url`` (+ optional
  ``label``). Returns ``{"slug": ..., "url": "/go/<slug>"}`` as JSON so
  the admin-page button can drop the short link straight into the
  clipboard.
* ``GET /go/{slug}`` — 302 redirect to the stored ``target_url`` after
  bumping the ``hits`` counter. Unknown / malformed slugs 404 without
  leaking which is which.
* ``GET /permalinks`` — admin table of the 50 most recent permalinks
  plus the JS button that captures ``window.location.href`` and POSTs
  it to ``/api/permalink``.

Validation of ``target_url`` (must start with ``/``) lives in
:mod:`app.permalinks`. The route layer translates HTTP <-> the helpers
and renders the template.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import permalinks as permalinks_store
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.permalinks")

router = APIRouter(tags=["permalinks"])

# Cap on the admin-table query — same number the helper defaults to.
_LIST_LIMIT: Final[int] = 50


@router.post("/api/permalink")
async def api_create_permalink(
    target_url: str = Form(...),
    label: str | None = Form(None),
) -> JSONResponse:
    """Mint a permalink for ``target_url`` and return ``{slug, url}``.

    Validation errors (empty URL, absolute URL, protocol-relative URL,
    URL too long) surface as 400 with the helper's message so the JS
    on the admin page can show it via ``alert``. Mint-failure (a
    collision storm we cannot resolve) surfaces as 500.
    """
    try:
        slug = await permalinks_store.create(target_url, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("permalinks.api_create_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        {
            "slug": slug,
            "url": f"/go/{slug}",
        }
    )


@router.get("/go/{slug}")
async def follow_permalink(slug: str) -> RedirectResponse:
    """302-redirect to the stored ``target_url`` and bump ``hits``.

    Unknown or malformed slugs 404 — the helper returns ``None`` for
    both cases so we cannot leak which one the operator hit.
    """
    row = await permalinks_store.get(slug)
    if row is None:
        log.info("permalinks.miss", slug=slug)
        raise HTTPException(status_code=404, detail="Permalink not found")

    # ``bump_hits`` is intentionally fire-and-await: it's a single
    # parametrised UPDATE on an indexed PK so the latency is negligible
    # compared with the round-trip the browser is about to make for
    # the redirect target.
    await permalinks_store.bump_hits(slug)

    target = str(row["target_url"])
    log.info("permalinks.follow", slug=slug, target_url=target)
    # 302 keeps the redirect cacheless so updates to the target (in
    # future revisions of the row, should we ever add edit support)
    # take effect immediately.
    return RedirectResponse(url=target, status_code=302)


@router.get("/permalinks", response_class=HTMLResponse)
async def admin_permalinks(request: Request) -> HTMLResponse:
    """Render the admin table + create-from-current-URL button."""
    rows = await permalinks_store.list_recent(_LIST_LIMIT)
    return templates.TemplateResponse(
        request,
        "permalinks.html",
        {
            "title": "Permalinks",
            "active_nav": "settings",
            "rows": rows,
        },
    )


__all__ = ["router"]
