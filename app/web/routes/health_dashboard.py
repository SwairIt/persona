"""``/admin/health`` worker heartbeat dashboard + JSON probe.

Reads :func:`app.workers.heartbeat.get_all` and exposes it twice:

* ``GET /admin/health`` — a Tailwind page with a worker grid and a
  green/amber/red dot per row driven by ``seconds_since``.
* ``GET /api/health.json`` — the same payload as JSON, suitable for
  external probes (k8s, uptime monitors, the Doctor page, etc.).

The colour thresholds live in this module so the JSON probe and the
HTML page can never drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.time import iso
from app.web.templates_engine import templates
from app.workers.heartbeat import HeartbeatRow, get_all

router = APIRouter(tags=["admin"])

log = get_logger("persona.heartbeat")

GREEN_THRESHOLD_SECONDS: Final[float] = 120.0
AMBER_THRESHOLD_SECONDS: Final[float] = 600.0


def _freshness(seconds_since: float) -> str:
    """Map ``seconds_since`` to ``green`` / ``amber`` / ``red``."""
    if seconds_since < 0:
        return "red"
    if seconds_since < GREEN_THRESHOLD_SECONDS:
        return "green"
    if seconds_since < AMBER_THRESHOLD_SECONDS:
        return "amber"
    return "red"


def _decorate(rows: list[HeartbeatRow]) -> list[dict[str, object]]:
    """Attach a ``freshness`` label so the template stays template-only."""
    decorated: list[dict[str, object]] = []
    for row in rows:
        decorated.append(
            {
                "name": row["name"],
                "last_run_at": row["last_run_at"],
                "last_status": row["last_status"],
                "ticks": row["ticks"],
                "seconds_since": row["seconds_since"],
                "freshness": _freshness(row["seconds_since"]),
            }
        )
    return decorated


@router.get("/admin/health", response_class=HTMLResponse)
async def health_dashboard_page(request: Request) -> HTMLResponse:
    """Render the per-worker heartbeat grid."""
    rows = await get_all()
    decorated = _decorate(rows)
    summary = {
        "green": sum(1 for r in decorated if r["freshness"] == "green"),
        "amber": sum(1 for r in decorated if r["freshness"] == "amber"),
        "red": sum(1 for r in decorated if r["freshness"] == "red"),
    }
    return templates.TemplateResponse(
        request,
        "health_dashboard.html",
        {
            "title": "Worker health",
            "active_nav": "settings",
            "rows": decorated,
            "summary": summary,
            "now_iso": iso(datetime.now(timezone.utc)),
            "green_threshold": GREEN_THRESHOLD_SECONDS,
            "amber_threshold": AMBER_THRESHOLD_SECONDS,
        },
    )


@router.get("/api/health.json")
async def health_json() -> JSONResponse:
    """Return every worker heartbeat as JSON for external probes."""
    rows = await get_all()
    decorated = _decorate(rows)
    payload: dict[str, object] = {
        "now": iso(datetime.now(timezone.utc)),
        "workers": decorated,
        "thresholds": {
            "green_seconds": GREEN_THRESHOLD_SECONDS,
            "amber_seconds": AMBER_THRESHOLD_SECONDS,
        },
    }
    return JSONResponse(payload)


__all__ = ["router"]
