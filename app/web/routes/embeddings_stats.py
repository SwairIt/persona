"""Embeddings stats panel — HTML page + JSON API.

v0.80 surfaces the existing :mod:`app.embeddings_stats` snapshot under
two URLs:

* ``GET /stats/embeddings`` renders a Tailwind page with four stat
  cards (eligible shots, embedded count, percentage, last re-index).
* ``GET /api/embeddings-stats.json`` returns the same snapshot as JSON
  for the dashboard's auto-refresh and ad-hoc scripting.

This module is a thin presentation shell — all the SQL lives in
:mod:`app.embeddings_stats`. The route layer only formats values for
the template (timestamps, em-dashes for ``None``) and never touches the
DB directly.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.embeddings_stats import EmbeddingsStats, compute_embeddings_stats
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.embeddings.stats")

router = APIRouter(tags=["embeddings-stats"])


def _format_timestamp(iso_value: str | None) -> str:
    """Render an ISO-8601 timestamp as ``YYYY-MM-DD HH:MM:SS``.

    Returns an em-dash when the value is missing or unparseable — the
    page's stat card should never show a raw "None" string and a bad kv
    row must not break the render. ``datetime.fromisoformat`` accepts
    both naive and timezone-aware strings, so we strip the tz for a
    consistent display (the same convention as
    :func:`app.web.templates_engine._format_human_time`).
    """
    if iso_value is None:
        return "—"
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        log.warning("embeddings.stats.bad_timestamp", value=iso_value)
        return iso_value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _build_context(stats: EmbeddingsStats) -> dict[str, object]:
    """Pack the snapshot + presentation strings for the template."""
    pending = max(0, stats["total_shots"] - stats["embedded_count"])
    return {
        "title": "Embeddings stats",
        "active_nav": "stats",
        "total_shots": stats["total_shots"],
        "embedded_count": stats["embedded_count"],
        "embedded_pct": stats["embedded_pct"],
        "pending": pending,
        "model": stats["model"],
        "dimension": stats["dimension"],
        "dimension_display": (
            str(stats["dimension"]) if stats["dimension"] is not None else "—"
        ),
        "last_reindex_at": stats["last_reindex_at"],
        "last_reindex_display": _format_timestamp(stats["last_reindex_at"]),
    }


@router.get("/stats/embeddings", response_class=HTMLResponse)
async def embeddings_stats_page(request: Request) -> HTMLResponse:
    """Render the four-card Tailwind page."""
    stats = await compute_embeddings_stats()
    return templates.TemplateResponse(
        request,
        "embeddings_stats.html",
        _build_context(stats),
    )


@router.get("/api/embeddings-stats.json", response_class=JSONResponse)
async def embeddings_stats_json() -> JSONResponse:
    """Return the snapshot as JSON for auto-refresh + ad-hoc scripting."""
    stats = await compute_embeddings_stats()
    pending = max(0, stats["total_shots"] - stats["embedded_count"])
    payload: dict[str, object] = {
        "total_shots": stats["total_shots"],
        "embedded_count": stats["embedded_count"],
        "embedded_pct": stats["embedded_pct"],
        "pending": pending,
        "model": stats["model"],
        "dimension": stats["dimension"],
        "last_reindex_at": stats["last_reindex_at"],
    }
    return JSONResponse(payload)
