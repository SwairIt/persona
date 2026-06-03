"""HTML endpoint for the day-vs-day concatenated OCR diff (Persona v0.78).

Renders the unified diff (text) plus a side-by-side HtmlDiff table for
two calendar days at ``GET /stats/day-ocr-diff?a=YYYY-MM-DD&b=YYYY-MM-DD``.

The backend work lives in :func:`app.day_ocr_diff.diff_days_ocr`, which
returns just the unified-diff string. This route adds a sibling
:class:`difflib.HtmlDiff` rendering so the template can offer both views
without the helper having to know anything about HTML.
"""

from __future__ import annotations

import difflib
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.day_ocr_diff import diff_days_ocr
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.day_ocr_diff")

router = APIRouter(tags=["stats"])


def _default_days() -> tuple[str, str]:
    """Return ``(yesterday, today)`` ISO dates in the local timezone.

    Used when the caller omits ``a`` / ``b`` so the page has a sane
    landing view. Going via :meth:`date.fromordinal` handles month / year
    rollover without pulling in :mod:`datetime.timedelta`.
    """
    today = datetime.now().astimezone().date()
    yesterday = date.fromordinal(today.toordinal() - 1)
    return yesterday.isoformat(), today.isoformat()


def _resolve_days(a: str | None, b: str | None) -> tuple[str, str]:
    """Fall back to ``(yesterday, today)`` when either param is missing."""
    default_a, default_b = _default_days()
    return (a or default_a, b or default_b)


def _html_table(unified: str, day_a: str, day_b: str) -> str:
    """Render a side-by-side :class:`difflib.HtmlDiff` from a unified diff.

    We rebuild the per-side line lists from the unified-diff string so
    the helper can stay strictly text-only while the route still offers
    a side-by-side panel. Pure :mod:`difflib`, no third-party deps.

    Lines starting with ``+`` belong only to B, ``-`` only to A, and any
    other line (context, hunk header, file header) belongs to both — we
    skip ``---``/``+++``/``@@`` markers so they don't pollute either
    column.
    """
    lines_a: list[str] = []
    lines_b: list[str] = []
    for line in unified.splitlines():
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith("+") and not line.startswith("++"):
            lines_b.append(line[1:])
        elif line.startswith("-") and not line.startswith("--"):
            lines_a.append(line[1:])
        else:
            # leading space == unified-diff context line; strip it once.
            stripped = line[1:] if line.startswith(" ") else line
            lines_a.append(stripped)
            lines_b.append(stripped)

    return difflib.HtmlDiff(wrapcolumn=80).make_table(
        lines_a,
        lines_b,
        fromdesc=day_a,
        todesc=day_b,
        context=False,
        numlines=2,
    )


@router.get("/stats/day-ocr-diff", response_class=HTMLResponse)
async def day_ocr_diff_page(
    request: Request,
    a: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    b: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
) -> HTMLResponse:
    """Render the day-vs-day concatenated OCR diff page.

    Returns ``400`` when either date fails ISO parsing — defers all
    actual validation to :func:`diff_days_ocr` and translates its
    :class:`ValueError` into an HTTP error.
    """
    day_a, day_b = _resolve_days(a, b)

    try:
        unified = await diff_days_ocr(day_a, day_b)
    except ValueError as exc:
        log.info("day_ocr_diff.bad_date", a=day_a, b=day_b, error=str(exc))
        raise HTTPException(
            status_code=400,
            detail="Both 'a' and 'b' must be YYYY-MM-DD dates",
        ) from exc

    identical = unified == ""
    unified_lines = unified.splitlines() if unified else []
    html_table = "" if identical else _html_table(unified, day_a, day_b)

    log.info(
        "day_ocr_diff.render",
        day_a=day_a,
        day_b=day_b,
        identical=identical,
        unified_lines=len(unified_lines),
    )

    return templates.TemplateResponse(
        request,
        "day_ocr_diff.html",
        {
            "title": f"Day OCR diff {day_a} vs {day_b}",
            "active_nav": "stats",
            "day_a": day_a,
            "day_b": day_b,
            "identical": identical,
            "unified_lines": unified_lines,
            "html_table": html_table,
        },
    )
