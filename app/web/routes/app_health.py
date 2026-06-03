"""Per-app health dashboard — ``/stats/app-health`` HTML + JSON probe.

Persona v0.69 feature 1/3.

Wraps :func:`app.app_health.compute_app_health` in a thin FastAPI layer
that renders a sortable Tailwind table and a JSON counterpart for
external monitors. The colour thresholds for the OCR failure column
live in this module so the JSON probe and the HTML page can never
drift apart — both consume :func:`_classify_fail_rate`.

The route deliberately does not re-derive aggregates from the database
itself; the data layer owns the SQL and the route owns the
presentation. Tests can exercise either half in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.app_health import AppHealthRow, compute_app_health
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.app_health")

# Window-validation mirrors :mod:`app.app_health` so a bad query-string
# returns 422 before the data layer is touched.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 7

# Quick-select buttons for the window switcher — same shape as the
# OCR-error-rate page so muscle memory carries across the two stats
# views.
_WINDOW_CHOICES: Final[tuple[int, ...]] = (1, 7, 14, 30, 90)

# OCR-failure colour thresholds. Below ``GREEN_PCT`` the cell is green
# (healthy), between ``GREEN_PCT`` and ``AMBER_PCT`` it's amber
# (degrading), at or above ``AMBER_PCT`` it's red (broken). Cells with
# no completed OCR in the window are styled as ``"none"`` (zinc) so the
# operator can tell "no data" apart from "no failures".
GREEN_PCT: Final[float] = 5.0
AMBER_PCT: Final[float] = 15.0


def _classify_fail_rate(
    row: AppHealthRow,
) -> str:
    """Bucket the OCR fail rate into ``green`` / ``amber`` / ``red`` / ``none``.

    ``none`` is reserved for apps whose window saw zero captures *or*
    zero completed OCR — there's nothing to colour confidently in that
    case. The bucket name is consumed by the template's Tailwind class
    lookup and by the JSON probe.
    """
    if row["shots_7d"] == 0:
        return "none"
    pct = row["ocr_fail_rate_pct"]
    if pct >= AMBER_PCT:
        return "red"
    if pct >= GREEN_PCT:
        return "amber"
    return "green"


def _seconds_since(last_seen: str) -> float | None:
    """Compute the age of ``last_seen`` in seconds, ``None`` on parse error.

    The stored value is the ISO form ``captured_at`` was inserted with
    by the capture worker. SQLite's ``datetime('now')`` fallback used
    by very old rows is a space-separated form — handled by stripping
    to the date prefix when full ISO parse fails.
    """
    try:
        parsed = datetime.fromisoformat(last_seen)
    except ValueError:
        # SQLite's "YYYY-MM-DD HH:MM:SS" (no T, no offset). Try the
        # space form by replacing the separator before giving up.
        try:
            parsed = datetime.fromisoformat(last_seen.replace(" ", "T"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - parsed
    return delta.total_seconds()


def _humanise_age(seconds: float | None) -> str:
    """Format an age in seconds as a short relative string.

    Used by the dashboard's ``Last seen`` column so the operator can
    glance and tell "just now" from "two weeks ago". Falls back to a
    literal ``"—"`` when parsing failed.
    """
    if seconds is None:
        return "—"
    if seconds < 0:
        # Clock skew between capture host and dashboard host — show
        # something neutral rather than a negative "-3s ago" string.
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _decorate(rows: list[AppHealthRow]) -> list[dict[str, Any]]:
    """Attach presentation helpers so the template stays declarative."""
    decorated: list[dict[str, Any]] = []
    for row in rows:
        age = _seconds_since(row["last_seen"])
        decorated.append(
            {
                "app_name": row["app_name"],
                "last_seen": row["last_seen"],
                "last_seen_human": _humanise_age(age),
                "last_seen_seconds": age,
                "shots_7d": row["shots_7d"],
                "ocr_fail_rate_pct": row["ocr_fail_rate_pct"],
                "bucket": _classify_fail_rate(row),
            }
        )
    return decorated


@router.get("/stats/app-health", response_class=HTMLResponse)
async def app_health_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-app health dashboard."""
    rows = await compute_app_health(days=days)
    decorated = _decorate(rows)

    summary = {
        "apps": len(decorated),
        "total_shots": sum(r["shots_7d"] for r in decorated),
        "red": sum(1 for r in decorated if r["bucket"] == "red"),
        "amber": sum(1 for r in decorated if r["bucket"] == "amber"),
        "green": sum(1 for r in decorated if r["bucket"] == "green"),
    }

    log.info(
        "app_health.page",
        days=days,
        apps=summary["apps"],
        total_shots=summary["total_shots"],
        red=summary["red"],
        amber=summary["amber"],
    )

    return templates.TemplateResponse(
        request,
        "app_health.html",
        {
            "title": "App health",
            "active_nav": "stats",
            "days": days,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "window_choices": _WINDOW_CHOICES,
            "rows": decorated,
            "summary": summary,
            "green_pct": GREEN_PCT,
            "amber_pct": AMBER_PCT,
        },
    )


@router.get("/api/app-health.json", response_class=JSONResponse)
async def app_health_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the per-app health rows as JSON.

    The payload echoes the resolved ``days`` window plus the green /
    amber thresholds so an external monitor can tag rows the same way
    the HTML view does without re-deriving the cutoffs.
    """
    rows = await compute_app_health(days=days)
    payload_rows: list[dict[str, Any]] = []
    for row in rows:
        bucket = _classify_fail_rate(row)
        payload_rows.append(
            {
                "app_name": row["app_name"],
                "last_seen": row["last_seen"],
                "shots_7d": row["shots_7d"],
                "ocr_fail_rate_pct": row["ocr_fail_rate_pct"],
                "bucket": bucket,
            }
        )
    payload: dict[str, Any] = {
        "days": days,
        "green_pct": GREEN_PCT,
        "amber_pct": AMBER_PCT,
        "apps": len(payload_rows),
        "rows": payload_rows,
    }
    return JSONResponse(payload)
