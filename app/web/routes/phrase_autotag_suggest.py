"""Admin UI — OCR phrase auto-tag rule suggestions.

Renders a Tailwind table of high-frequency phrases that aren't already covered
by an ``ocr_phrase_tag`` rule. Each row carries an *Adopt* form that POSTs
straight to the existing ``/settings/phrase-tags`` endpoint — we deliberately
don't add a new write path, so the same validation, audit and redirect
behaviour applies.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.phrase_autotag_suggest import suggest_rules
from app.web.templates_engine import templates

log = get_logger("persona.phrase_autotag_suggest")

router = APIRouter(tags=["ocr-phrase-tags"])

# Selector buckets surfaced in the toolbar — any other value coerces to the
# closest member so a hand-edited URL can't blow the look-back window open.
_ALLOWED_DAYS: tuple[int, ...] = (7, 30, 90)
_ALLOWED_TOP_N: tuple[int, ...] = (10, 20, 50)
_ALLOWED_N_GRAM: tuple[int, ...] = (2, 3)


def _snap(value: int, allowed: tuple[int, ...]) -> int:
    """Return the closest member of ``allowed`` to ``value``."""
    return min(allowed, key=lambda candidate: abs(candidate - value))


@router.get("/admin/phrase-tags/suggestions", response_class=HTMLResponse)
async def phrase_tags_suggestions_page(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    top: int = Query(default=20, ge=1, le=200),
    n: int = Query(default=2, ge=2, le=3),
) -> HTMLResponse:
    """Render the suggestions table.

    Each row exposes an Adopt form which POSTs ``phrase`` + ``tag`` to the
    existing :func:`app.web.routes.ocr_phrase_tags.phrase_tags_create`
    handler — no new write surface is introduced here.
    """
    days_choice = _snap(days, _ALLOWED_DAYS)
    top_choice = _snap(top, _ALLOWED_TOP_N)
    n_gram_choice = _snap(n, _ALLOWED_N_GRAM)

    suggestions = await suggest_rules(
        days=days_choice,
        top_n=top_choice,
        n_gram=n_gram_choice,
    )

    log.info(
        "phrase_autotag_suggest.page",
        days=days_choice,
        top=top_choice,
        n_gram=n_gram_choice,
        returned=len(suggestions),
    )

    return templates.TemplateResponse(
        request,
        "phrase_autotag_suggest.html",
        {
            "title": "Phrase auto-tag suggestions",
            "active_nav": "settings",
            "suggestions": suggestions,
            "days": days_choice,
            "top": top_choice,
            "n_gram": n_gram_choice,
            "allowed_days": _ALLOWED_DAYS,
            "allowed_top": _ALLOWED_TOP_N,
            "allowed_n_gram": _ALLOWED_N_GRAM,
        },
    )
