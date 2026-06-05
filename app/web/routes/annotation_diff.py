"""HTTP surface for the annotation revision diff (v1.46).

Two endpoints layered on top of :mod:`app.annotation_diff`:

- ``GET /screenshot/{shot_id}/annotation-diff?a=...&b=...`` — HTML
  side-by-side view rendering revision A on top, revision B beneath,
  with removed primitives highlighted red in A and added primitives
  highlighted green in B. A legend at the top spells out the colour
  semantics and shows the bucket counts.
- ``GET /api/screenshot/{shot_id}/annotation-diff.json?a=...&b=...`` —
  the same diff in machine-readable form for tests / future tooling.
  The raw SVG payloads are NOT echoed back here (they can be fetched
  through the existing autosave revision endpoints); only the bucketed
  added/removed elements and the kept count.

Architectural note: this router lives in its own file so the surgical
v1.46 addition does not bloat the existing annotation modules. It is
intentionally NOT imported by ``app/web/main.py`` from this code path
— registration happens at app startup via the project's standard
router-include convention.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.annotation_diff import compute_revision_diff
from app.logging_setup import get_logger
from app.shot_annotations import sanitise_svg
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["annotation-diff"])
log = get_logger("persona.web.annotation_diff")


async def _require_screenshot(shot_id: int) -> None:
    """Raise 404 if ``shot_id`` does not exist."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")


def _validate_revision_pair(a_id: int, b_id: int) -> None:
    """Reject same-id comparisons up front with a clear error.

    Diffing a revision against itself is technically valid (everything
    is "kept") but never useful, and the URL is almost certainly a copy-
    paste mistake. Surfacing it as a 400 saves a confused user from a
    blank-looking diff page.
    """
    if int(a_id) == int(b_id):
        raise HTTPException(
            status_code=400,
            detail="a and b must be different revision ids",
        )


@router.get(
    "/api/screenshot/{shot_id}/annotation-diff.json",
)
async def annotation_diff_json(
    shot_id: int,
    a: int = Query(..., description="First revision id"),
    b: int = Query(..., description="Second revision id"),
) -> JSONResponse:
    """Return the bucketed diff as JSON (no raw SVG payloads).

    The diff buckets are the same dicts the HTML view consumes, just
    without the two ``rev_*_svg`` fields — those would balloon the
    response and are already available through the existing autosave
    revision endpoints if a client really needs them.
    """
    await _require_screenshot(shot_id)
    _validate_revision_pair(a, b)
    try:
        diff = await compute_revision_diff(shot_id, a, b)
    except ValueError as exc:
        log.info(
            "annotation_diff.api.revision_missing",
            shot_id=shot_id,
            a=a,
            b=b,
            error=str(exc),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return JSONResponse(
        {
            "shot_id": diff["shot_id"],
            "rev_a_id": diff["rev_a_id"],
            "rev_b_id": diff["rev_b_id"],
            "rev_a_saved_at": diff["rev_a_saved_at"],
            "rev_b_saved_at": diff["rev_b_saved_at"],
            "added": diff["added"],
            "removed": diff["removed"],
            "kept_count": diff["kept_count"],
        }
    )


@router.get(
    "/screenshot/{shot_id}/annotation-diff",
    response_class=HTMLResponse,
)
async def annotation_diff_view(
    request: Request,
    shot_id: int,
    a: int = Query(..., description="First revision id"),
    b: int = Query(..., description="Second revision id"),
) -> HTMLResponse:
    """Render the side-by-side diff page.

    Both payloads are run through :func:`sanitise_svg` before being
    injected into the template — the revision rows were sanitised on
    write, but a second pass costs nothing and protects against a row
    that landed before the sanitiser was tightened.
    """
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    _validate_revision_pair(a, b)

    try:
        diff = await compute_revision_diff(shot_id, a, b)
    except ValueError as exc:
        log.info(
            "annotation_diff.view.revision_missing",
            shot_id=shot_id,
            a=a,
            b=b,
            error=str(exc),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return templates.TemplateResponse(
        request,
        "annotation_diff.html",
        {
            "title": "Аннотация diff",
            "active_nav": "timeline",
            "shot": shot,
            "rev_a_id": diff["rev_a_id"],
            "rev_b_id": diff["rev_b_id"],
            "rev_a_saved_at": diff["rev_a_saved_at"],
            "rev_b_saved_at": diff["rev_b_saved_at"],
            "rev_a_svg": sanitise_svg(diff["rev_a_svg"]),
            "rev_b_svg": sanitise_svg(diff["rev_b_svg"]),
            "added": diff["added"],
            "removed": diff["removed"],
            "kept_count": diff["kept_count"],
        },
    )
