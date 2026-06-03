"""Per-screenshot summary endpoint for the delete-preview modal.

Persona v1.7 feature 3/3. The delete-preview JS
(``/static/delete_preview.js``) intercepts clicks on
``data-delete-shot-id`` controls, opens a confirmation modal, and needs
a small JSON snapshot of the row to show inside it. We keep that
payload in a single dedicated endpoint so the modal is cheap to
populate without re-using the full ``/screenshot/{id}`` HTML page or
leaking any extra fields a screenshot row carries.

Endpoint
--------
``GET /api/screenshot/{screenshot_id}/summary.json``
    Returns ``{"id", "app_name", "captured_at", "ocr_preview",
    "thumbnail_url"}`` for one screenshot. Missing rows produce a
    plain 404 — the JS treats anything non-2xx as "skip the modal and
    fall through to a vanilla ``confirm()``".

The OCR preview is a short single-line excerpt (whitespace collapsed,
truncated to ``_OCR_PREVIEW_LIMIT`` chars with an ellipsis when cut)
so the modal stays compact and never reveals more text than fits on
one line. ``captured_at`` is rendered as an ISO-8601 string so the
client can format it with the user's locale without us guessing.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.routes.thumbnails import thumbnail_url

log = get_logger("persona.delete_preview")

router = APIRouter(tags=["delete_preview"])

# Cap on the OCR excerpt rendered inside the modal. Long enough to give
# the operator a recognisable hint about what they are about to delete
# but short enough to never wrap to a second line on a narrow viewport.
_OCR_PREVIEW_LIMIT: int = 160

# Ellipsis we append when the OCR text is longer than the cap. Unicode
# "…" so the trimmed string remains a single rendered glyph.
_OCR_PREVIEW_ELLIPSIS: str = "…"

# Whitespace collapse: one or more whitespace characters → a single
# space. Pre-compiled so we are not rebuilding the regex on every
# request. Keeps the preview rendering deterministic regardless of how
# many newlines/tabs the OCR pipeline left in the stored text.
_WHITESPACE_RE: re.Pattern[str] = re.compile(r"\s+")


def _build_ocr_preview(text: str | None) -> str:
    """Collapse whitespace and truncate ``text`` for the modal preview.

    Returns an empty string when the OCR text is missing or all
    whitespace — the client side treats an empty preview as "no OCR"
    and hides the row entirely, which is cleaner than showing a blank
    placeholder.
    """
    if not text:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", text).strip()
    if not collapsed:
        return ""
    if len(collapsed) <= _OCR_PREVIEW_LIMIT:
        return collapsed
    # Cut at the limit minus one so the appended ellipsis keeps the
    # total length equal to ``_OCR_PREVIEW_LIMIT`` — a UI that sizes
    # itself off the cap therefore never wraps.
    return collapsed[: _OCR_PREVIEW_LIMIT - 1].rstrip() + _OCR_PREVIEW_ELLIPSIS


@router.get(
    "/api/screenshot/{screenshot_id}/summary.json",
    response_class=JSONResponse,
)
async def screenshot_summary_json(
    screenshot_id: Annotated[int, Path(ge=1)],
) -> JSONResponse:
    """Return a compact snapshot of one screenshot for the delete modal."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        log.info("delete_preview.summary.missing", screenshot_id=screenshot_id)
        raise HTTPException(status_code=404, detail="Screenshot not found")

    payload: dict[str, object] = {
        "id": shot.id,
        "app_name": shot.app_name,
        "captured_at": shot.captured_at.isoformat(),
        "ocr_preview": _build_ocr_preview(shot.ocr_text),
        "thumbnail_url": thumbnail_url(shot.thumbnail_path),
    }
    log.info(
        "delete_preview.summary.served",
        screenshot_id=screenshot_id,
        has_thumbnail=payload["thumbnail_url"] is not None,
        ocr_preview_chars=len(str(payload["ocr_preview"])),
    )
    return JSONResponse(payload)
