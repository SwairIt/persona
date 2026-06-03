"""Admin page + endpoint for swapping Tesseract OCR with the vision pass.

Persona v0.75 feature 2/3. The v0.55 vision fallback
(:mod:`app.llm.ocr_via_vision`) writes its transcription into a sidecar
column ``screenshots.ocr_text_vision`` so the canonical Tesseract column
``ocr_text`` stays untouched and the two signals stay independent.

For shots where vision clearly read the screen better than Tesseract did,
the operator wants a one-click way to *promote* the vision result into
the canonical column so the rest of Persona (search, embeddings,
exports, digests, tag rules) consumes the better transcription without
having to special-case the sidecar everywhere.

This module exposes that promotion as a deliberate, audit-logged admin
action behind a scary red button:

* ``GET  /admin/ocr-vision-replace``       — preview page showing the
  count of shots that have *both* a non-NULL ``ocr_text`` and a non-NULL
  ``ocr_text_vision`` (i.e. eligible for promotion).
* ``POST /admin/ocr-vision-replace/apply`` — copy ``ocr_text_vision``
  into ``ocr_text`` for every such row, audit-log the action, redirect
  back to the preview page with a confirmation banner.

Safety + design notes
---------------------
* **Opt-in.** No automatic promotion. The button explicitly overwrites
  Tesseract output, which is destructive — the operator must click.
* **Eligibility filter.** Only rows with *both* columns non-NULL are
  touched. Rows where vision returned an empty string (cached negative
  result) are explicitly excluded so a click can't blank out perfectly
  good Tesseract text.
* **Parametrised SQL.** No values are ever interpolated into the SQL
  string; SQLite's ``UPDATE ... FROM`` is not used, just a self-scoped
  ``WHERE`` clause on the same table.
* **Audit-logged.** Each successful POST writes an :mod:`app.audit` row
  with the affected count and the resolved actor (client host) so a
  ``/audit`` reader can trace exactly when and how many rows got
  promoted.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-vision-replace"])
log = get_logger("persona.ocr.vision_replace")


# Eligibility predicate, inlined into both the preview-count and the
# apply UPDATE so the two never drift. A row is eligible when it has a
# Tesseract result *and* a vision result; the vision result must be
# non-empty so we never clobber Tesseract with an empty negative-cache
# string. No values are ever interpolated into either query string — the
# predicate is a pure constant — so parametrised-SQL safety is preserved.
_COUNT_SQL = (
    "SELECT COUNT(*) AS n FROM screenshots "
    "WHERE ocr_text IS NOT NULL "
    "AND ocr_text_vision IS NOT NULL "
    "AND ocr_text_vision <> ''"
)
_APPLY_SQL = (
    "UPDATE screenshots "
    "SET ocr_text = ocr_text_vision "
    "WHERE ocr_text IS NOT NULL "
    "AND ocr_text_vision IS NOT NULL "
    "AND ocr_text_vision <> ''"
)


async def _count_eligible() -> int:
    """Return the number of shots eligible for vision-text promotion."""
    async with get_connection() as conn:
        cursor = await conn.execute(_COUNT_SQL)
        row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


@router.get("/admin/ocr-vision-replace", response_class=HTMLResponse)
async def ocr_vision_replace_page(
    request: Request,
    affected: int | None = Query(default=None, ge=0),
) -> HTMLResponse:
    """Render the preview page with the eligible-row count + Apply button.

    ``affected`` is populated by the POST redirect so the page can show a
    confirmation banner ("promoted X rows") without server-side flash
    state.
    """
    eligible = await _count_eligible()
    return templates.TemplateResponse(
        request,
        "ocr_vision_replace.html",
        {
            "title": "OCR vision replace",
            "active_nav": "settings",
            "eligible": eligible,
            "affected": affected,
        },
    )


@router.post("/admin/ocr-vision-replace/apply")
async def ocr_vision_replace_apply(request: Request) -> RedirectResponse:
    """Copy ``ocr_text_vision`` into ``ocr_text`` for every eligible row.

    Eligibility matches :data:`_APPLY_SQL` — both columns non-NULL,
    vision result non-empty. The UPDATE is one statement so SQLite gives
    us a transactional all-or-nothing swap of the affected rows.

    Returns a 303 redirect back to the preview page with ``?affected=N``
    so the page can render a confirmation banner.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(_APPLY_SQL)
        await conn.commit()
        affected = cursor.rowcount or 0

    actor = request.client.host if request.client is not None else None

    log.info(
        "ocr.vision_replace.apply",
        affected=affected,
        actor=actor,
    )
    await log_action(
        action="ocr.vision_replace",
        actor=actor,
        target="screenshots",
        detail=f"affected={affected}",
        success=True,
    )

    return RedirectResponse(
        url=f"/admin/ocr-vision-replace?affected={affected}",
        status_code=303,
    )


__all__ = ["router"]
