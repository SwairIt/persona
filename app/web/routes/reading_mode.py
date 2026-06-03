"""Per-shot reading mode — chrome-free OCR view tuned for phone reading.

v0.53 feature 2/3. Renders the redacted OCR text of a single screenshot in
a minimalist, standalone HTML page — no nav, no Tailwind, no JS. The goal
is to give the user a quiet "Safari Reader"-style surface for catching up
on whatever long-form article, chat log, or PDF was on screen at capture
time, without the rest of the Persona dashboard fighting for attention on
a small display.

Design rules:

* The template does NOT extend ``base.html``. It is fully self-contained
  with an inline ``<style>`` block so the page renders identically when
  saved to disk, screenshotted by an external reader, or opened in a
  stripped-down in-app webview.
* The OCR text is always pushed through :func:`app.redaction.apply_redaction`
  before reaching the template — the same masking the search index sees.
  Showing un-redacted text on a "share-friendly" surface would be a
  regression on the privacy contract documented in ``app/redaction.py``.
* ``?mode=dark|light|auto`` swaps the colour palette. Anything else falls
  back to ``auto`` so a stray query value never 500s and never leaves the
  user staring at an unreadable page.

This module deliberately does NOT register itself with the FastAPI app in
:mod:`app.web.main`; the v0.53 task spec forbids touching that file. Wire
it up in a follow-up patch with::

    from app.web.routes import reading_mode as reading_mode_routes
    app.include_router(reading_mode_routes.router)
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

log = get_logger("persona.reading_mode")

router = APIRouter(tags=["reading-mode"])

ReadingTheme = Literal["dark", "light", "auto"]
_VALID_MODES: frozenset[str] = frozenset({"dark", "light", "auto"})
_DEFAULT_MODE: ReadingTheme = "auto"


def _normalise_mode(value: str | None) -> ReadingTheme:
    """Clamp the ``?mode=`` query value to a known theme.

    Forgiving on purpose: a typo in a copy-pasted URL should still render
    a usable page, just at the default palette. The bad value is logged
    so a frequently-mistyped option can be surfaced later.
    """
    if value is None:
        return _DEFAULT_MODE
    candidate = value.strip().lower()
    if candidate in _VALID_MODES:
        return candidate  # type: ignore[return-value]  # Literal narrowed by membership check
    log.info("reading_mode.bad_mode_param", mode=value)
    return _DEFAULT_MODE


@router.get("/shot/{screenshot_id}/reader", response_class=HTMLResponse)
async def reader_page(
    request: Request,
    screenshot_id: int,
    mode: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the chrome-free reading view for a single screenshot.

    Returns 404 if the screenshot row is missing — there is nothing to
    read. A row with no OCR text yet (``ocr_status`` still ``pending``
    or ``skipped``) renders the empty-state copy rather than 404ing so
    the user can bookmark the URL and revisit once OCR catches up.
    """
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    raw_text = shot.ocr_text or ""
    clean_text, masks_applied = await apply_redaction(raw_text)
    theme = _normalise_mode(mode)

    log.info(
        "reading_mode.rendered",
        screenshot_id=screenshot_id,
        mode=theme,
        chars=len(clean_text),
        masks_applied=masks_applied,
        ocr_status=shot.ocr_status,
    )

    return templates.TemplateResponse(
        request,
        "reading_mode.html",
        {
            "title": f"Reader · shot #{screenshot_id}",
            "screenshot_id": screenshot_id,
            "app_name": shot.app_name,
            "captured_at": shot.captured_at,
            "ocr_text": clean_text,
            "ocr_status": shot.ocr_status,
            "mode": theme,
        },
    )


__all__ = ["router"]
