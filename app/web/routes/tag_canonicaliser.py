"""HTMX-driven admin UI for :mod:`app.tag_canonicaliser`.

Three endpoints, all under ``/admin/tag-canonical`` / ``/api/tag-canonical``:

* ``GET  /admin/tag-canonical``       — render the page with the current
  cluster preview + an Apply button. No DB writes; equivalent to the
  ``preview`` POST below so refreshing the page always shows the live
  state.
* ``POST /api/tag-canonical/preview`` — return the dry-run preview
  fragment (or full JSON, depending on Accept).
* ``POST /api/tag-canonical/apply``   — fold every cluster into its
  canonical winner, return the same fragment but flipped to "applied"
  mode so the operator can confirm the rewrite landed.

The file deliberately stays a thin adapter: all the cluster-picking,
SQL and transaction logic lives in :mod:`app.tag_canonicaliser`. We
just shuttle the result dict into the template context (or back as
JSON) and emit one structured-log event per request.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.tag_canonicaliser import apply_canonicalisation
from app.web.templates_engine import templates

log = get_logger("persona.web.tag_canonicaliser")

router = APIRouter(tags=["tag-canonicaliser"])

# Mirrors the default in :mod:`app.tag_canonicaliser`. Surfaced as a
# query-string knob so an operator can broaden the sweep on a low-
# volume install (``?min_count=1``) without editing code.
_DEFAULT_MIN_COUNT = 2


def _clamp_min_count(value: int | None) -> int:
    """Coerce the query-string ``min_count`` to a sane non-zero int."""
    if value is None:
        return _DEFAULT_MIN_COUNT
    if value < 1:
        return 1
    return int(value)


@router.get("/admin/tag-canonical", response_class=HTMLResponse)
async def tag_canonical_page(
    request: Request,
    min_count: int | None = Query(default=None, ge=0),
) -> HTMLResponse:
    """Render the canonicaliser page with a freshly-computed dry-run preview.

    The preview is computed on every GET so the operator sees the
    current state of the tag clusters — no stale snapshot. Rendering
    is cheap (one ``GROUP BY`` + N COUNTs); apply is the expensive
    path and is gated behind the POST endpoint below.
    """
    floor = _clamp_min_count(min_count)
    result = await apply_canonicalisation(dry_run=True, min_count=floor)
    log.info(
        "tag_canonical.page",
        min_count=floor,
        clusters=result["clusters"],
        rows_updated=result["rows_updated"],
    )
    return templates.TemplateResponse(
        request,
        "tag_canonicaliser.html",
        {
            "title": "Tag canonicaliser",
            "active_nav": "settings",
            "min_count": floor,
            "result": result,
            "applied": False,
        },
    )


@router.post("/api/tag-canonical/preview", response_model=None)
async def tag_canonical_preview(
    request: Request,
    min_count: int | None = Query(default=None, ge=0),
) -> HTMLResponse | JSONResponse:
    """Return a fresh dry-run preview.

    HTMX callers (``Accept: text/html``) receive the template fragment
    so the page can swap a single ``#tag-canonical-result`` block;
    every other caller receives the raw JSON ``ApplyResult`` for
    scripted use.
    """
    floor = _clamp_min_count(min_count)
    result = await apply_canonicalisation(dry_run=True, min_count=floor)
    log.info(
        "tag_canonical.preview",
        min_count=floor,
        clusters=result["clusters"],
        rows_updated=result["rows_updated"],
    )
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return templates.TemplateResponse(
            request,
            "tag_canonicaliser.html",
            {
                "title": "Tag canonicaliser",
                "active_nav": "settings",
                "min_count": floor,
                "result": result,
                "applied": False,
            },
        )
    return JSONResponse(content=dict(result))


@router.post("/api/tag-canonical/apply", response_model=None)
async def tag_canonical_apply(
    request: Request,
    min_count: int | None = Query(default=None, ge=0),
) -> HTMLResponse | JSONResponse:
    """Commit the canonicalisation sweep and re-render the page.

    After the apply lands we run another dry-run so the operator sees
    that every cluster is now collapsed (the post-apply preview should
    be empty unless an alias re-appeared mid-flight). HTMX callers
    receive the template fragment; everyone else receives the JSON
    ``ApplyResult`` from the apply call itself, with ``rows_updated``
    set to the real number of rewrites that landed.
    """
    floor = _clamp_min_count(min_count)
    applied = await apply_canonicalisation(dry_run=False, min_count=floor)
    log.info(
        "tag_canonical.apply",
        min_count=floor,
        clusters=applied["clusters"],
        rows_updated=applied["rows_updated"],
    )
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        # Re-run the dry path so the operator sees the post-apply
        # state in the same view (should be empty unless someone is
        # racing us by re-introducing folded spellings).
        fresh = await apply_canonicalisation(dry_run=True, min_count=floor)
        return templates.TemplateResponse(
            request,
            "tag_canonicaliser.html",
            {
                "title": "Tag canonicaliser",
                "active_nav": "settings",
                "min_count": floor,
                "result": fresh,
                "applied": True,
                "applied_clusters": applied["clusters"],
                "applied_rows": applied["rows_updated"],
                "applied_preview": applied["preview"],
            },
        )
    return JSONResponse(content=dict(applied))


__all__ = ["router"]
