"""Consolidated health dashboard — workers + DB + budget + audit + flags.

Three new endpoints back the unified Tailwind page:

* ``GET /health-dashboard``                  → full HTML page (extends base.html)
* ``GET /api/health-dashboard.json``         → machine-readable :class:`HealthState`
* ``GET /api/health-dashboard/fragment``     → ``_health_fragment.html`` for HTMX polling

The legacy worker-only endpoints are kept for backward compatibility
with external uptime probes:

* ``GET /admin/health``               → redirects to the new dashboard
* ``GET /api/health.json``            → workers-only narrow JSON (legacy probe)

The new page reads its data through :func:`app.health_dashboard.build_health_state`;
this module is a thin view layer that does no SQL of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.health_dashboard import (
    AMBER_THRESHOLD_SECONDS,
    GREEN_THRESHOLD_SECONDS,
    build_health_state,
)
from app.logging_setup import get_logger
from app.storage.time import iso
from app.web.templates_engine import templates
from app.workers.heartbeat import HeartbeatRow, get_all

router = APIRouter(tags=["admin"])

log = get_logger("persona.health_dashboard")

# Legacy worker-only thresholds preserved for ``/api/health.json`` callers.
_LEGACY_GREEN_THRESHOLD_SECONDS: Final[float] = GREEN_THRESHOLD_SECONDS
_LEGACY_AMBER_THRESHOLD_SECONDS: Final[float] = AMBER_THRESHOLD_SECONDS


def _legacy_freshness(seconds_since: float) -> str:
    """Map ``seconds_since`` to ``green`` / ``amber`` / ``red`` for the legacy JSON probe."""
    if seconds_since < 0:
        return "red"
    if seconds_since < _LEGACY_GREEN_THRESHOLD_SECONDS:
        return "green"
    if seconds_since < _LEGACY_AMBER_THRESHOLD_SECONDS:
        return "amber"
    return "red"


def _legacy_decorate(rows: list[HeartbeatRow]) -> list[dict[str, object]]:
    """Attach a ``freshness`` label to each heartbeat row for the legacy JSON probe."""
    decorated: list[dict[str, object]] = []
    for row in rows:
        decorated.append(
            {
                "name": row["name"],
                "last_run_at": row["last_run_at"],
                "last_status": row["last_status"],
                "ticks": row["ticks"],
                "seconds_since": row["seconds_since"],
                "freshness": _legacy_freshness(row["seconds_since"]),
            }
        )
    return decorated


# ---------------------------------------------------------------------
# Consolidated dashboard — new endpoints
# ---------------------------------------------------------------------


@router.get("/health-dashboard", response_class=HTMLResponse)
async def health_dashboard_page(request: Request) -> HTMLResponse:
    """Render the full consolidated dashboard page."""
    state = await build_health_state()
    return templates.TemplateResponse(
        request,
        "health_dashboard.html",
        {
            "title": "Здоровье",
            "active_nav": "settings",
            "health": state,
            "green_threshold": GREEN_THRESHOLD_SECONDS,
            "amber_threshold": AMBER_THRESHOLD_SECONDS,
        },
    )


@router.get("/api/health-dashboard.json")
async def health_dashboard_json(
    _user: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Return the full :class:`HealthState` as JSON (owner-only)."""
    state = await build_health_state()
    return JSONResponse(dict(state))


@router.get("/api/health-dashboard/fragment", response_class=HTMLResponse)
async def health_dashboard_fragment(request: Request) -> HTMLResponse:
    """Return only the inner dashboard partial — HTMX polls this every 30s."""
    state = await build_health_state()
    return templates.TemplateResponse(
        request,
        "_health_fragment.html",
        {
            "health": state,
            "green_threshold": GREEN_THRESHOLD_SECONDS,
            "amber_threshold": AMBER_THRESHOLD_SECONDS,
        },
    )


# ---------------------------------------------------------------------
# Legacy endpoints — kept for backward compatibility
# ---------------------------------------------------------------------


@router.get("/admin/health", response_class=HTMLResponse)
async def legacy_admin_health() -> RedirectResponse:
    """Redirect the legacy admin URL to the consolidated dashboard."""
    return RedirectResponse(url="/health-dashboard", status_code=307)


@router.get("/api/health.json")
async def health_json() -> JSONResponse:
    """Return every worker heartbeat as JSON for external probes.

    This is the narrow, worker-only shape consumed by uptime monitors
    and the Doctor page — kept stable on purpose. New callers should
    prefer ``/api/health-dashboard.json``.
    """
    rows = await get_all()
    decorated = _legacy_decorate(rows)
    payload: dict[str, object] = {
        "now": iso(datetime.now(UTC)),
        "workers": decorated,
        "thresholds": {
            "green_seconds": _LEGACY_GREEN_THRESHOLD_SECONDS,
            "amber_seconds": _LEGACY_AMBER_THRESHOLD_SECONDS,
        },
    }
    return JSONResponse(payload)


__all__ = ["router"]
