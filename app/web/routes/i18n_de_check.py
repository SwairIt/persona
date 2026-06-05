"""Admin page: per-language i18n coverage table.

A tiny diagnostic surface for translators and QA. Now that Persona ships
three locales (``en`` / ``ru`` / ``de``) and the catalog has grown past
~290 keys, eyeballing two JSON files no longer scales — a partially
translated locale silently falls back to English via :func:`app.i18n.t`,
so missing keys never surface in normal use.

This route exposes the truth: for every loaded locale, how many keys it
has and exactly which keys it is missing relative to the English source
of truth. English itself is the baseline (zero "missing" — its catalog
*defines* the set).

Mounted under ``/admin/i18n-coverage`` so it sits beside the other
small admin diagnostics and stays out of the public nav. ``active_nav``
points to the Settings tab — the page is conceptually a translator's
view of the language picker that lives in Settings.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.i18n import (
    _TRANSLATIONS,
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

_log = get_logger("persona.web.i18n_coverage")

router = APIRouter(tags=["admin", "i18n"])

# Stable display order: English first (the baseline), then the other
# supported locales alphabetised. Any locale that's loaded but *not*
# whitelisted in :data:`SUPPORTED_LANGUAGES` (e.g. a half-finished
# translation dropped into the folder) is appended at the end so the
# admin can see it exists without it polluting the supported set.
_BASE_LANG: Final[str] = DEFAULT_LANGUAGE


def _build_coverage_rows() -> list[dict[str, object]]:
    """Compute one row per loaded locale for the coverage table.

    Pulled out of the request handler so it's pure-data and trivial to
    test directly without spinning up an HTTP client. Reads the frozen
    ``_TRANSLATIONS`` cache populated at import time in :mod:`app.i18n`.
    """
    baseline_keys: frozenset[str] = frozenset(
        _TRANSLATIONS.get(_BASE_LANG, {}).keys()
    )
    baseline_total = len(baseline_keys)

    supported_loaded = sorted(
        lang for lang in _TRANSLATIONS if lang in SUPPORTED_LANGUAGES
    )
    extra_loaded = sorted(
        lang for lang in _TRANSLATIONS if lang not in SUPPORTED_LANGUAGES
    )
    # English first, then the rest of the supported set, then any stray
    # locales — see module docstring for the rationale.
    ordered: list[str] = []
    if _BASE_LANG in supported_loaded:
        ordered.append(_BASE_LANG)
    ordered.extend(lang for lang in supported_loaded if lang != _BASE_LANG)
    ordered.extend(extra_loaded)

    rows: list[dict[str, object]] = []
    for lang in ordered:
        table = _TRANSLATIONS.get(lang, {})
        table_keys = frozenset(table.keys())
        missing = sorted(baseline_keys - table_keys)
        coverage_pct = (
            100.0
            if baseline_total == 0
            else round(100.0 * (baseline_total - len(missing)) / baseline_total, 1)
        )
        rows.append(
            {
                "lang": lang,
                "supported": lang in SUPPORTED_LANGUAGES,
                "is_base": lang == _BASE_LANG,
                "total": len(table),
                "missing": missing,
                "missing_count": len(missing),
                "coverage_pct": coverage_pct,
            }
        )
    return rows


@router.get("/admin/i18n-coverage", response_class=HTMLResponse)
async def i18n_coverage_page(request: Request) -> HTMLResponse:
    """Render the per-language coverage table."""
    rows = _build_coverage_rows()
    baseline_total = len(_TRANSLATIONS.get(_BASE_LANG, {}))
    _log.info(
        "i18n_coverage.viewed",
        locales=[row["lang"] for row in rows],
        baseline_total=baseline_total,
    )
    return templates.TemplateResponse(
        request,
        "i18n_coverage.html",
        {
            "title": "i18n coverage",
            "active_nav": "settings",
            "rows": rows,
            "baseline_lang": _BASE_LANG,
            "baseline_total": baseline_total,
            "supported_languages": sorted(SUPPORTED_LANGUAGES),
        },
    )


__all__ = ["router"]
