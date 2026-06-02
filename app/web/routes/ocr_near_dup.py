"""Admin page + soft-delete endpoint for OCR near-duplicate review.

Persona v0.44 feature 2/3. Pairs go through :func:`app.ocr_near_dup.find_near_duplicates`,
get rendered as a side-by-side thumbnail table, and the admin clicks
"Keep A / delete B", "Keep B / delete A", or "Keep both" per pair.

Deletes route through :func:`app.recycle.soft_delete_screenshot` (the
v0.40 recycle bin), never a raw ``DELETE FROM screenshots`` — so the
admin can always restore a row if the dedup decision was wrong.

Routes
------
* ``GET  /admin/ocr-near-duplicates``          — render the page.
* ``POST /admin/ocr-near-duplicates/delete``   — soft-delete one shot;
  redirects back with the same ``days`` / ``min_jaccard`` filter so the
  user keeps reviewing without losing context.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.ocr_near_dup import find_near_duplicates
from app.recycle import soft_delete_screenshot
from app.web.templates_engine import templates

log = get_logger("persona.ocr.near_dup")

router = APIRouter(tags=["ocr-near-dup"])

# Defaults that mirror :func:`find_near_duplicates` — kept in sync via
# the same constants the underlying module already clamps to, so a
# bad querystring hitting either layer behaves identically.
_DEFAULT_DAYS = 7
_DEFAULT_JACCARD = 0.85
_DEFAULT_MAX_PAIRS = 200


@router.get("/admin/ocr-near-duplicates", response_class=HTMLResponse)
async def ocr_near_dup_page(
    request: Request,
    days: Annotated[int, Query(ge=1, le=3650)] = _DEFAULT_DAYS,
    min_jaccard: Annotated[float, Query(ge=0.05, le=1.0)] = _DEFAULT_JACCARD,
) -> HTMLResponse:
    """Render the near-duplicate pairs review page.

    Both filter knobs are query-string parameters so the admin can
    bookmark a configuration and the "delete" POST can redirect back
    to the exact same view.
    """
    pairs = await find_near_duplicates(
        days=days,
        min_jaccard=min_jaccard,
        max_pairs=_DEFAULT_MAX_PAIRS,
    )
    log.info(
        "ocr.near_dup.page.render",
        days=days,
        min_jaccard=min_jaccard,
        pairs=len(pairs),
    )
    return templates.TemplateResponse(
        request,
        "ocr_near_dup.html",
        {
            "title": "OCR near-duplicates",
            "active_nav": "settings",
            "pairs": pairs,
            "days": days,
            "min_jaccard": min_jaccard,
            "max_pairs": _DEFAULT_MAX_PAIRS,
        },
    )


@router.post("/admin/ocr-near-duplicates/delete")
async def ocr_near_dup_delete(
    shot_id: Annotated[int, Form(ge=1)],
    days: Annotated[int, Form(ge=1, le=3650)] = _DEFAULT_DAYS,
    min_jaccard: Annotated[float, Form(ge=0.05, le=1.0)] = _DEFAULT_JACCARD,
) -> RedirectResponse:
    """Soft-delete one screenshot via the recycle bin.

    Returns 404 when the shot id is unknown — :func:`soft_delete_screenshot`
    distinguishes a missing row from a real failure by returning ``None``,
    which we promote to an HTTP error so the admin sees the issue rather
    than a silent redirect.

    On success the audit log records ``ocr.near_dup.delete`` with the
    shot id so a later forensic review can reconstruct which dedup
    decisions the admin made.
    """
    recycle_id = await soft_delete_screenshot(shot_id)
    if recycle_id is None:
        await log_action(
            "ocr.near_dup.delete",
            target=str(shot_id),
            detail="not found",
            success=False,
        )
        raise HTTPException(status_code=404, detail="Screenshot not found")

    await log_action(
        "ocr.near_dup.delete",
        target=str(shot_id),
        detail=f"recycle_id={recycle_id}",
    )
    log.info(
        "ocr.near_dup.deleted",
        shot_id=shot_id,
        recycle_id=recycle_id,
    )
    # Round-trip the filter so the admin lands back on the same view.
    # ``min_jaccard`` is a float — format with enough precision to keep
    # the URL stable across reloads but not so much it looks like noise.
    redirect_url = f"/admin/ocr-near-duplicates?days={days}&min_jaccard={min_jaccard:.3f}"
    return RedirectResponse(url=redirect_url, status_code=303)
