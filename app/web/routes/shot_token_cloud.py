"""Per-shot OCR token cloud — v0.61.

Visualises the words in a single screenshot's OCR text as a size-weighted
tag cloud. Font-size scales with frequency on a logarithmic scale so a
handful of dominant tokens don't drown out the long tail.

Stopwords (English + Russian function words plus OCR/web noise) are reused
from :mod:`app.keywords` so the v0.28 filter list is the single source of
truth — no drift between the weekly tag cloud and this per-shot view.

Clicking any rendered word jumps to ``/search?q=<word>`` so the operator can
pivot from "this is what's on screen" to "every other screenshot that
mentioned this term".
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.keywords import STOPWORDS
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

log = get_logger("persona.shot_cloud")

router = APIRouter(tags=["shot-token-cloud"])

# Font-size band (rem). Matches the weekly /keywords cloud so visual
# weight feels consistent across the two pages.
_FONT_MIN_REM: float = 0.85
_FONT_MAX_REM: float = 2.40

# Drop tokens shorter than this after cleaning — single-letter glyphs and
# two-letter prepositions add noise without information.
_MIN_TOKEN_LENGTH: int = 4

# Hard upper bound on rendered tags so a wall-of-text screenshot can't
# generate a 10k-word cloud that DOSes the browser.
_MAX_TOKENS: int = 200


def _tokenise(text: str) -> list[str]:
    """Split ``text`` on whitespace, strip non-alphanumeric, lowercase.

    Unicode-aware (``str.isalnum`` accepts Cyrillic) so Russian and English
    flow through the same path. Mirrors :func:`app.keywords._tokenise` but
    is duplicated here to keep this module self-contained — the original is
    a private helper and we don't want to import a leading underscore name.
    """
    tokens: list[str] = []
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append(cleaned.lower())
    return tokens


def _count_tokens(ocr_text: str) -> Counter[str]:
    """Tokenise ``ocr_text`` and return a stopword-filtered frequency map."""
    counter: Counter[str] = Counter()
    for token in _tokenise(ocr_text):
        if len(token) < _MIN_TOKEN_LENGTH or token in STOPWORDS:
            continue
        counter[token] += 1
    return counter


def _decorate(items: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """Attach pre-computed ``size_rem`` / ``weight`` / ``opacity`` per item.

    Visual weight is precomputed on a log scale so the template just
    interpolates numbers — Jinja2 has no ``log`` filter and we'd rather not
    drop a custom one in for one page.
    """
    if not items:
        return []

    max_count = max(count for _, count in items)
    log_max = math.log(max_count) if max_count > 1 else 1.0

    decorated: list[dict[str, Any]] = []
    for word, count in items:
        ratio = math.log(count) / log_max if max_count > 1 and count > 0 else 1.0
        ratio = max(0.0, min(1.0, ratio))
        size_rem = _FONT_MIN_REM + ratio * (_FONT_MAX_REM - _FONT_MIN_REM)
        weight = 400 + round(ratio * 5) * 100
        opacity = 0.55 + ratio * 0.45
        decorated.append(
            {
                "word": word,
                "count": count,
                "size_rem": round(size_rem, 2),
                "weight": int(weight),
                "opacity": round(opacity, 2),
            }
        )
    return decorated


@router.get("/screenshot/{screenshot_id}/cloud", response_class=HTMLResponse)
async def shot_token_cloud(
    request: Request,
    screenshot_id: int,
) -> HTMLResponse:
    """Render the per-shot token cloud for ``screenshot_id``.

    Returns 404 when the screenshot doesn't exist. A screenshot with empty
    or missing OCR text still renders the page but with an empty-state
    panel — the operator gets a clear "no text yet" rather than a blank
    cloud they have to interpret.
    """
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    ocr_text = shot.ocr_text or ""
    counter = _count_tokens(ocr_text)
    top = counter.most_common(_MAX_TOKENS)
    items = _decorate(top)

    log.info(
        "shot_cloud.rendered",
        screenshot_id=screenshot_id,
        ocr_chars=len(ocr_text),
        unique_tokens=len(counter),
        rendered=len(items),
    )

    return templates.TemplateResponse(
        request,
        "shot_token_cloud.html",
        {
            "title": f"Token cloud #{screenshot_id}",
            "active_nav": "timeline",
            "shot": shot,
            "items": items,
            "unique_tokens": len(counter),
            "ocr_chars": len(ocr_text),
        },
    )
