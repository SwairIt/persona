"""``GET /stats/llm-usage`` — per-day LLM token-spend dashboard.

Reads the ``llm_usage`` ledger populated by
:class:`app.llm.client._UsageRecordingClient` and renders a 30-day
table + sparkline. A JSON sibling at ``/api/llm-usage.json`` returns
the same payload for ad-hoc scripting.

The page is deliberately read-only — there is no delete-row or
clear-history endpoint here. The ledger is small (one INTEGER and a
short TEXT slug per LLM call) and operators have been clear they would
rather audit a full history than save a few KB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, TypedDict

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["llm-usage"])

log = get_logger("persona.llm_usage")

#: 30-day default window matches the screenshot-budget / storage-savings
#: dashboards so the operator's mental model stays consistent.
_DEFAULT_DAYS = 30

#: Hard cap on the ``?days=`` query parameter. Anything beyond 365 days
#: gets clamped — a year of LLM calls is enough history for any honest
#: question and prevents a malicious query from materialising an
#: arbitrarily large rowset.
_MAX_DAYS = 365


class _DayRow(TypedDict):
    day: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


class _KindRow(TypedDict):
    kind: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


def _clamp_days(value: int) -> int:
    if value < 1:
        return 1
    if value > _MAX_DAYS:
        return _MAX_DAYS
    return value


def _window_start_iso(days: int) -> str:
    """Return the ISO-8601 ``datetime('now', '-N days')`` cutoff.

    SQLite's ``datetime('now')`` is wall-clock UTC, so we build the
    cutoff in Python the same way (``datetime.now(UTC)``) and let the
    ``ts >= ?`` comparison run as a pure string compare on the indexed
    column.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


async def _collect(days: int) -> dict[str, Any]:
    """Gather aggregated rows + per-day series + per-kind breakdown.

    Three queries, all parametrised on the ``ts`` cutoff:

    1. ``daily`` — one row per calendar day with summed input / output
       tokens and call count.
    2. ``per_kind`` — one row per ``kind`` slug (digest / vision / …)
       for the breakdown table at the bottom of the page.
    3. ``totals`` — grand totals card at the top.

    Days with zero recorded calls are filled in client-side so the
    sparkline x-axis is contiguous even on a fresh install.
    """
    cutoff_ts = _window_start_iso(days)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(ts) AS day, "
            "       COUNT(*) AS calls, "
            "       COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "       COALESCE(SUM(output_tokens), 0) AS output_tokens "
            "FROM llm_usage "
            "WHERE ts >= ? "
            "GROUP BY day "
            "ORDER BY day",
            (cutoff_ts,),
        )
        daily_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT kind, "
            "       COUNT(*) AS calls, "
            "       COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "       COALESCE(SUM(output_tokens), 0) AS output_tokens "
            "FROM llm_usage "
            "WHERE ts >= ? "
            "GROUP BY kind "
            "ORDER BY (COALESCE(SUM(input_tokens), 0) + "
            "          COALESCE(SUM(output_tokens), 0)) DESC, kind",
            (cutoff_ts,),
        )
        kind_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "       SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failed "
            "FROM llm_usage "
            "WHERE ts >= ?",
            (cutoff_ts,),
        )
        totals_row = await cursor.fetchone()

    # Fill in zero-call days so the sparkline x-axis is contiguous.
    today = date.today()
    by_day: dict[str, _DayRow] = {}
    for offset in range(days):
        day_iso = (today - timedelta(days=days - 1 - offset)).isoformat()
        by_day[day_iso] = {
            "day": day_iso,
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
    for row in daily_rows:
        day = str(row["day"])
        if day not in by_day:
            # Row older than the visible window because of DST or a
            # clock skew at insert time. Drop it rather than skewing
            # the chart left.
            continue
        input_tokens = int(row["input_tokens"]) if row["input_tokens"] else 0
        output_tokens = int(row["output_tokens"]) if row["output_tokens"] else 0
        by_day[day] = {
            "day": day,
            "calls": int(row["calls"]),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    series: list[_DayRow] = list(by_day.values())
    max_total = max((row["total_tokens"] for row in series), default=0)

    by_kind: list[_KindRow] = []
    for row in kind_rows:
        input_tokens = int(row["input_tokens"]) if row["input_tokens"] else 0
        output_tokens = int(row["output_tokens"]) if row["output_tokens"] else 0
        by_kind.append(
            {
                "kind": str(row["kind"]),
                "calls": int(row["calls"]),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        )

    total_calls = int(totals_row["calls"]) if totals_row else 0
    total_input = (
        int(totals_row["input_tokens"])
        if totals_row and totals_row["input_tokens"]
        else 0
    )
    total_output = (
        int(totals_row["output_tokens"])
        if totals_row and totals_row["output_tokens"]
        else 0
    )
    total_failed = (
        int(totals_row["failed"]) if totals_row and totals_row["failed"] else 0
    )

    return {
        "days_window": days,
        "series": series,
        "max_total": max_total,
        "by_kind": by_kind,
        "total_calls": total_calls,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_failed": total_failed,
    }


@router.get("/stats/llm-usage", response_class=HTMLResponse)
async def llm_usage_page(
    request: Request,
    days: int = Query(_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the Tailwind dashboard with summary cards + 30-day SVG line."""
    window = _clamp_days(days)
    payload = await _collect(window)
    log.info(
        "llm_usage.page.rendered",
        days=window,
        total_calls=payload["total_calls"],
        total_tokens=payload["total_tokens"],
    )
    return templates.TemplateResponse(
        request,
        "llm_usage.html",
        {
            "title": "LLM usage",
            "active_nav": "stats",
            **payload,
        },
    )


@router.get("/api/llm-usage.json", response_class=JSONResponse)
async def llm_usage_json(
    days: int = Query(_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
) -> JSONResponse:
    """Same payload as the HTML page, for ad-hoc scripting / dashboards."""
    window = _clamp_days(days)
    payload = await _collect(window)
    return JSONResponse(payload)


__all__ = ["router"]
