"""Admin UI for the privacy-mode sentinel log (v1.40).

Read-only counterpart to :mod:`app.web.routes.capture_blocklist_admin`.
The regex blocklist has writeable per-rule rows; privacy mode does
not — its pattern list lives in :mod:`app.privacy_mode` as a Python
constant, by design (a kv toggle that an attacker can flip via the
admin UI would defeat the purpose of a privacy mode). The operator
disables the feature globally by clearing
:data:`app.privacy_mode.PRIVACY_PATTERNS` and redeploying.

Routes:

    GET  /privacy-mode                — HTML status page (counts + catalogue)
    GET  /api/privacy-mode/stats.json — JSON counters for tooling

No POSTs, no per-pattern toggle, no row deletion — the audit trail is
append-only by policy.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.privacy_mode import PRIVACY_PATTERNS, stats
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.privacy_mode.admin")

router = APIRouter(tags=["privacy-mode"])


@router.get("/privacy-mode", response_class=HTMLResponse)
async def privacy_mode_page(request: Request) -> HTMLResponse:
    """Render the privacy-mode status page.

    Shows today / 7-day skip counters, the per-pattern breakdown for
    the last week, and the read-only catalogue of sentinel patterns.
    """
    async with get_connection() as conn:
        summary = await stats(conn)
    return templates.TemplateResponse(
        request,
        "privacy_mode.html",
        {
            "title": "Privacy mode",
            "active_nav": "settings",
            "today": summary["today"],
            "last7d": summary["last7d"],
            "by_pattern": summary["by_pattern"],
            "patterns": list(PRIVACY_PATTERNS),
        },
    )


@router.get("/api/privacy-mode/stats.json")
async def privacy_mode_stats_json() -> JSONResponse:
    """Return the same counters as the HTML page, as JSON.

    Shape: ``{today: int, last7d: int, by_pattern: [{pattern, count}, ...]}``.
    Used by external tooling that wants to graph privacy-skip activity
    without scraping the admin page.
    """
    async with get_connection() as conn:
        summary = await stats(conn)
    return JSONResponse(
        {
            "today": summary["today"],
            "last7d": summary["last7d"],
            "by_pattern": summary["by_pattern"],
        }
    )


__all__ = ["router"]
