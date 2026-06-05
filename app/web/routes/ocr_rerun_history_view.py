"""HTTP surface for the per-shot OCR re-run revision log + diff viewer.

Persona v1.46 feature. Four endpoints:

* ``GET  /screenshot/{shot_id}/ocr-history``
    HTML page listing every recorded OCR revision for one screenshot
    in newest-first order. Each row carries a ``run_at`` stamp, a
    ``char_count`` figure and a ``run_source`` badge; the rendering
    template adds a "Compare" form that picks any two rows and posts
    to the diff endpoint below.
* ``GET  /screenshot/{shot_id}/ocr-diff?a=<rev_a>&b=<rev_b>``
    HTML page rendering the unified-diff between two revisions of one
    screenshot's OCR text. Lines starting with ``"+"`` / ``"-"`` are
    colour-styled in the template so the operator can see exactly
    what changed.
* ``GET  /api/screenshot/{shot_id}/ocr-history.json``
    JSON list of the same revision rows for programmatic clients.
* ``GET  /api/screenshot/{shot_id}/ocr-diff.json``
    JSON projection of :class:`app.ocr_rerun_history.DiffResult` for
    the same ``a`` / ``b`` query pair.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — per task spec, ``main.py`` is off-limits. Wire
it up with::

    from app.web.routes import ocr_rerun_history_view as ocr_rerun_history_view_routes
    app.include_router(ocr_rerun_history_view_routes.router)

Design notes
------------
* **All SQL goes through :mod:`app.ocr_rerun_history`.** This route
  layer never builds SQL — parametrisation lives in the helper module
  so a future schema change has one place to update.
* **404 maps cleanly to a missing screenshot.** A ``shot_id`` with no
  revisions in the log is rendered as an empty table, not a 404 —
  that's a legitimate state (no re-runs have happened yet). Only the
  diff endpoint can 404, and only when *both* requested revisions are
  missing.
* **Diff math is sync.** :func:`difflib.unified_diff` is CPU-bound and
  tiny; the helper layer runs it inline. No background job, no
  caching — recomputing on each request keeps the surface honest.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr_rerun_history import compute_diff, list_revisions
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-rerun-history"])
log = get_logger("persona.ocr_rerun_history.route")


@router.get(
    "/screenshot/{shot_id}/ocr-history",
    response_class=HTMLResponse,
)
async def ocr_rerun_history_page(
    request: Request,
    shot_id: int,
) -> HTMLResponse:
    """Render the per-shot OCR revision log as an HTML table.

    An empty revision list is a legitimate state (no re-runs yet) and
    renders as an empty table with a friendly notice — we do NOT 404
    because the screenshot may still exist and the absence of history
    is itself information.
    """
    revisions = await list_revisions(shot_id)

    log.info(
        "ocr_rerun_history.page.rendered",
        shot_id=shot_id,
        count=len(revisions),
    )

    return templates.TemplateResponse(
        request,
        "ocr_rerun_history.html",
        {
            "title": "История OCR",
            "active_nav": "timeline",
            "shot_id": shot_id,
            "revisions": revisions,
        },
    )


@router.get(
    "/screenshot/{shot_id}/ocr-diff",
    response_class=HTMLResponse,
)
async def ocr_rerun_history_diff_page(
    request: Request,
    shot_id: int,
    a: int = Query(..., description="ocr_rerun_history.id of the FROM revision"),
    b: int = Query(..., description="ocr_rerun_history.id of the TO revision"),
) -> HTMLResponse:
    """Render the unified-diff between revisions ``a`` and ``b``.

    Both ids are looked up by the helper; a missing row yields the
    empty string for that side so the page still renders. The
    operator-visible "(revision deleted)" notice is left to a future
    iteration — keeping the route minimal here.
    """
    diff = await compute_diff(a, b)

    log.info(
        "ocr_rerun_history.diff_page.rendered",
        shot_id=shot_id,
        rev_id_a=a,
        rev_id_b=b,
        additions=diff["additions"],
        deletions=diff["deletions"],
    )

    return templates.TemplateResponse(
        request,
        "ocr_rerun_history_diff.html",
        {
            "title": f"OCR diff #{a} → #{b}",
            "active_nav": "timeline",
            "shot_id": shot_id,
            "rev_id_a": a,
            "rev_id_b": b,
            "diff": diff,
        },
    )


@router.get(
    "/api/screenshot/{shot_id}/ocr-history.json",
    response_class=JSONResponse,
)
async def ocr_rerun_history_json(shot_id: int) -> JSONResponse:
    """JSON projection of the per-shot revision log (newest-first)."""
    revisions = await list_revisions(shot_id)
    log.info(
        "ocr_rerun_history.api.list",
        shot_id=shot_id,
        count=len(revisions),
    )
    return JSONResponse({"shot_id": shot_id, "revisions": list(revisions)})


@router.get(
    "/api/screenshot/{shot_id}/ocr-diff.json",
    response_class=JSONResponse,
)
async def ocr_rerun_history_diff_json(
    shot_id: int,
    a: int = Query(..., description="ocr_rerun_history.id of the FROM revision"),
    b: int = Query(..., description="ocr_rerun_history.id of the TO revision"),
) -> JSONResponse:
    """JSON projection of :class:`DiffResult` for the (a, b) pair.

    Returns 404 only when neither revision exists — a one-sided
    missing row is still a meaningful diff (everything became
    insertions or everything became deletions) and we'd rather render
    it than refuse.
    """
    diff = await compute_diff(a, b)

    # A diff with zero lines AND zero additions AND zero deletions
    # means both sides resolved to the empty string — which in turn
    # means both revisions were missing. That's the only true 404
    # shape; an empty diff between two existing identical revisions
    # still returns 200 with ``lines: []``.
    if (
        not diff["lines"]
        and diff["additions"] == 0
        and diff["deletions"] == 0
    ):
        # Probe both ids; if BOTH are absent we 404, otherwise a true
        # no-op diff is the right answer.
        from app.ocr_rerun_history import _fetch_one_revision  # noqa: PLC0415

        rev_a = await _fetch_one_revision(a)
        rev_b = await _fetch_one_revision(b)
        if rev_a is None and rev_b is None:
            log.info(
                "ocr_rerun_history.api.diff.not_found",
                shot_id=shot_id,
                rev_id_a=a,
                rev_id_b=b,
            )
            raise HTTPException(
                status_code=404, detail="Neither revision exists"
            )

    log.info(
        "ocr_rerun_history.api.diff",
        shot_id=shot_id,
        rev_id_a=a,
        rev_id_b=b,
        additions=diff["additions"],
        deletions=diff["deletions"],
    )
    return JSONResponse(
        {
            "shot_id": shot_id,
            "rev_id_a": a,
            "rev_id_b": b,
            "additions": diff["additions"],
            "deletions": diff["deletions"],
            "lines": diff["lines"],
            "unified_diff_str": diff["unified_diff_str"],
        }
    )


__all__ = ["router"]
