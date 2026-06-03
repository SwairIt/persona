"""Per-shot iframe embed (v0.56).

Renders a minimal, chrome-free HTML page that external sites can drop
into an ``<iframe>`` to display a single Persona screenshot. Unlike
:mod:`app.web.routes.shot_share`, this endpoint is *not* token-gated:
embedding is a presentation concern, and the upstream caller already
decided to expose the screenshot id on a public surface (e.g. a public
share page or a blog post). Anyone who reaches the embed URL can render
it inside their own page.

Two affordances make the embed friendly to cross-origin hosts:

* ``X-Frame-Options: ALLOWALL`` overrides any future default ``DENY`` /
  ``SAMEORIGIN`` policy Persona might add — this single route opts in
  to being framed. We do not set ``Content-Security-Policy:
  frame-ancestors`` because we want the broadest browser support and
  the embed is intentionally public.
* ``?theme=dark|light`` lets the embedder pick a colour scheme that
  matches their site. Any other value falls back to ``dark`` (Persona's
  house default) rather than 400-ing — embeds should be forgiving.

OCR text is passed through :func:`app.redaction.apply_redaction` before
truncation so secrets caught by user-supplied rules never leak into a
third-party page, then clipped to 80 characters with an ellipsis. We
truncate *after* redaction so the mask token (``***``) cannot be sliced
in half by the length cap.
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

router = APIRouter(tags=["shot-embed"])
logger = get_logger("persona.embed")

# OCR snippet length shown inside the embed caption. The product brief
# fixes this at 80 characters; expressed as a constant so the template
# and any future tests can agree on the bound.
_OCR_SNIPPET_CHARS = 80

# Recognised values for ``?theme=``. Anything else degrades to the
# default rather than erroring — embeds must never 4xx because of a
# typo in the host page.
_VALID_THEMES: frozenset[str] = frozenset({"dark", "light"})
_DEFAULT_THEME: Literal["dark", "light"] = "dark"


def _truncate_ocr(text: str | None) -> str:
    """Return at most :data:`_OCR_SNIPPET_CHARS` characters of ``text``.

    A trailing ellipsis (``…``) replaces the final character when the
    input was longer than the cap, so the rendered caption visibly
    signals truncation rather than silently chopping mid-word.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= _OCR_SNIPPET_CHARS:
        return collapsed
    # Reserve one character for the ellipsis to keep the rendered length
    # exactly at the cap.
    return collapsed[: _OCR_SNIPPET_CHARS - 1] + "…"


def _normalise_theme(raw: str | None) -> Literal["dark", "light"]:
    """Coerce the ``?theme=`` query value into a known token.

    Unknown / missing values fall back to :data:`_DEFAULT_THEME`. The
    return type is narrowed to a ``Literal`` so the template context
    cannot accidentally receive an unsanitised string.
    """
    if raw is None:
        return _DEFAULT_THEME
    candidate = raw.strip().lower()
    if candidate in _VALID_THEMES:
        # ``candidate`` is provably one of the literal members at this
        # point; the explicit branches satisfy the type checker.
        if candidate == "light":
            return "light"
        return "dark"
    return _DEFAULT_THEME


@router.get("/screenshot/{screenshot_id}/embed", response_class=HTMLResponse)
async def embed_screenshot(
    request: Request,
    screenshot_id: int,
    theme: str | None = Query(default=None, description="dark | light"),
) -> HTMLResponse:
    """Render the standalone embeddable HTML for one screenshot."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        # 404 is appropriate even though the embed is unauthenticated —
        # the host page will see the iframe go blank, which is the
        # least-confusing failure mode.
        raise HTTPException(status_code=404, detail="Screenshot not found")

    resolved_theme = _normalise_theme(theme)

    redacted_ocr, masks_applied = await apply_redaction(shot.ocr_text or "")
    ocr_snippet = _truncate_ocr(redacted_ocr)

    logger.info(
        "embed.render",
        screenshot_id=screenshot_id,
        theme=resolved_theme,
        ocr_masks_applied=masks_applied,
        ocr_snippet_chars=len(ocr_snippet),
    )

    response = templates.TemplateResponse(
        request,
        "shot_embed.html",
        {
            "title": f"Screenshot #{shot.id}",
            "active_nav": "",
            "shot": shot,
            "ocr_snippet": ocr_snippet,
            "theme": resolved_theme,
        },
    )
    # ALLOWALL is the legacy "frame me from anywhere" value. Browsers
    # that no longer recognise it fall back to the absence of any
    # X-Frame-Options header, which also permits framing — exactly the
    # behaviour we want for a public embed endpoint.
    response.headers["X-Frame-Options"] = "ALLOWALL"
    return response
