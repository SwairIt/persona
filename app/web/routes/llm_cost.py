"""``GET /stats/llm-cost`` — per-day USD spend on BYO LLM calls.

Companion to :mod:`app.web.routes.llm_usage`:

* ``llm_usage`` answers *how many tokens did I burn?*
* ``llm_cost`` answers *how many dollars did I spend?*

Both pages read the same ledger (``llm_usage`` table, migration 079).
The dollar estimate is computed in :func:`app.llm_cost.compute_daily_llm_cost`
against a hardcoded price table — see that module for the (intentionally
approximate) numbers and the schema-vs-spec rationale.

Endpoints
---------
* ``GET /stats/llm-cost``         → HTML page (extends ``base.html``)
* ``GET /api/stats/llm-cost.json`` → same payload as JSON
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.llm_cost import DailyCostRow, compute_daily_llm_cost
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["llm-cost"])

log = get_logger("persona.web.llm_cost")

#: 30-day window — matches ``/stats/llm-usage`` so the operator's mental
#: model stays consistent across the two cost dashboards.
_DEFAULT_DAYS = 30

#: Hard cap on ``?days=`` to keep the rendered page bounded; copied from
#: :mod:`app.web.routes.llm_usage`.
_MAX_DAYS = 365


class _DayTotal(dict[str, Any]):
    """Type-friendly alias for the per-day aggregate dicts.

    Subclassing ``dict`` keeps it JSON-serialisable while still giving
    mypy a name to point at in :func:`_aggregate_by_day` signatures.
    """


def _aggregate_by_day(
    rows: list[DailyCostRow],
    days: int,
) -> list[dict[str, Any]]:
    """Collapse breakdown rows into one entry per calendar day.

    Returns ``days`` entries oldest-first so the SVG bar chart in the
    template can iterate ``loop.index0`` and get a contiguous x-axis even
    on a fresh install with zero recorded calls. Each entry carries:

    * ``day``           — ``YYYY-MM-DD``
    * ``calls``         — total calls across all (provider, kind) rows
    * ``input_total``   — summed input tokens
    * ``output_total``  — summed output tokens
    * ``est_cost_usd``  — summed estimated dollars
    """
    by_day: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "day": "",
            "calls": 0,
            "input_total": 0,
            "output_total": 0,
            "est_cost_usd": 0.0,
        }
    )

    # Seed every day in the window so missing days show up as zero bars
    # rather than gaps in the chart.
    today = date.today()
    for offset in range(days):
        day_iso = (today - timedelta(days=days - 1 - offset)).isoformat()
        by_day[day_iso] = {
            "day": day_iso,
            "calls": 0,
            "input_total": 0,
            "output_total": 0,
            "est_cost_usd": 0.0,
        }

    for row in rows:
        day = row["day"]
        bucket = by_day.get(day)
        if bucket is None:
            # Row older than the window because of DST / clock skew —
            # drop it rather than shifting the chart.
            continue
        bucket["calls"] += row["calls"]
        bucket["input_total"] += row["input_total"]
        bucket["output_total"] += row["output_total"]
        bucket["est_cost_usd"] += row["est_cost_usd"]

    # Oldest first for the chart.
    return [by_day[k] for k in sorted(by_day.keys())]


def _clamp_days(value: int) -> int:
    if value < 1:
        return 1
    if value > _MAX_DAYS:
        return _MAX_DAYS
    return value


async def _build_payload(days: int) -> dict[str, Any]:
    """Compose the template / JSON payload for a given window length."""
    window = _clamp_days(days)
    rows = await compute_daily_llm_cost(days=window)
    by_day = _aggregate_by_day(rows, window)

    total_usd = round(sum(r["est_cost_usd"] for r in rows), 4)
    total_calls = sum(r["calls"] for r in rows)
    active_days = sum(1 for d in by_day if d["calls"] > 0)
    max_day_usd = max((d["est_cost_usd"] for d in by_day), default=0.0)

    return {
        "days_window": window,
        "rows": rows,
        "by_day": by_day,
        "total_usd": total_usd,
        "total_calls": total_calls,
        "active_days": active_days,
        "max_day_usd": max_day_usd,
    }


@router.get("/stats/llm-cost", response_class=HTMLResponse)
async def llm_cost_page(
    request: Request,
    days: int = Query(_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-day LLM cost dashboard."""
    payload = await _build_payload(days)
    log.info(
        "llm_cost.page.rendered",
        days=payload["days_window"],
        total_usd=payload["total_usd"],
        rows=len(payload["rows"]),
    )
    return templates.TemplateResponse(
        request,
        "llm_cost.html",
        {
            "title": "Стоимость LLM",
            "active_nav": "stats",
            **payload,
        },
    )


@router.get("/api/stats/llm-cost.json", response_class=JSONResponse)
async def llm_cost_json(
    days: int = Query(_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
) -> JSONResponse:
    """Same payload as the HTML page, for ad-hoc scripting / dashboards."""
    payload = await _build_payload(days)
    return JSONResponse(payload)


__all__ = ["router"]
