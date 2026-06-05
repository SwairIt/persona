"""``/admin/heartbeat-alerts`` — operator view of silent workers.

Renders the output of :func:`app.worker_heartbeat_monitor.check_heartbeats`
three ways:

* ``GET /admin/heartbeat-alerts`` — Tailwind page extending ``base.html``.
  Shows every tracked worker with its expected cadence next to the
  current gap, plus the per-worker dedupe state ("last alerted at …"
  or "no alert pending").
* ``GET /api/heartbeat-alerts.json`` — same payload as JSON for external
  probes / scripted health checks.
* ``POST /api/heartbeat-alerts/clear-dedupe`` — drop every
  ``last_alerted_at_*`` row so the next monitor tick re-alerts on
  still-silent workers. Redirects back to the HTML page when called
  from a form; returns JSON otherwise.

The page does *not* push notifications itself — that's the alert
worker's job (see :mod:`app.workers.heartbeat_alert_worker`). The
route is a read-only window onto the same data the worker acts on, so
operators can sanity-check what the worker will send before the next
tick fires.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.time import iso
from app.web.templates_engine import templates
from app.worker_heartbeat_monitor import (
    EXPECTED_POLL_INTERVALS,
    HeartbeatAlert,
    check_heartbeats,
    clear_dedupe,
)

router = APIRouter(tags=["admin"])

log = get_logger("persona.heartbeat_alerts")


def _status_label(gap_seconds: float | None, threshold_seconds: float) -> str:
    """Map ``gap_seconds`` to a human label used by the template.

    ``unknown`` covers "no heartbeat row at all" (the worker never
    booted); ``stopped`` is "we have a beat, but it's older than the
    threshold"; ``ok`` is the implicit fallback for the JSON-only
    rows the monitor doesn't return — the HTML view backfills those
    explicitly so the table shows every tracked worker, not just the
    failing ones.
    """
    if gap_seconds is None:
        return "unknown"
    if gap_seconds > threshold_seconds:
        return "stopped"
    return "ok"


async def _build_view_rows() -> tuple[
    list[dict[str, object]], list[HeartbeatAlert]
]:
    """Return ``(rows, alerts)`` — table data plus the raw alert list.

    ``rows`` covers every entry in :data:`EXPECTED_POLL_INTERVALS` so
    the template can render the full inventory, not just the failing
    workers. ``alerts`` is the unmodified output of
    :func:`check_heartbeats` so the JSON endpoint can expose it
    verbatim alongside the dedupe state.
    """
    alerts = await check_heartbeats()
    alert_by_worker: dict[str, HeartbeatAlert] = {a["worker"]: a for a in alerts}

    rows: list[dict[str, object]] = []
    for worker, expected_poll_seconds in sorted(EXPECTED_POLL_INTERVALS.items()):
        alert = alert_by_worker.get(worker)
        if alert is None:
            # Worker is healthy — :func:`check_heartbeats` only returns
            # rows over the threshold, so we synthesise the row here.
            rows.append(
                {
                    "worker": worker,
                    "expected_poll_seconds": expected_poll_seconds,
                    "gap_seconds": None,
                    "threshold_seconds": round(3.0 * expected_poll_seconds, 3),
                    "last_beat_at": None,
                    "last_alerted_at": None,
                    "should_alert": False,
                    "status": "ok",
                }
            )
            continue

        rows.append(
            {
                "worker": worker,
                "expected_poll_seconds": expected_poll_seconds,
                "gap_seconds": alert["gap_seconds"],
                "threshold_seconds": alert["threshold_seconds"],
                "last_beat_at": alert["last_beat_at"],
                "last_alerted_at": alert["last_alerted_at"],
                "should_alert": alert["should_alert"],
                "status": _status_label(
                    alert["gap_seconds"], alert["threshold_seconds"]
                ),
            }
        )
    return rows, alerts


@router.get("/admin/heartbeat-alerts", response_class=HTMLResponse)
async def heartbeat_alerts_page(request: Request) -> HTMLResponse:
    """Render the per-worker heartbeat alert table."""
    rows, alerts = await _build_view_rows()
    summary = {
        "tracked": len(rows),
        "stopped": sum(1 for r in rows if r["status"] == "stopped"),
        "unknown": sum(1 for r in rows if r["status"] == "unknown"),
        "pending": sum(1 for a in alerts if a["should_alert"]),
    }
    return templates.TemplateResponse(
        request,
        "heartbeat_alerts.html",
        {
            "title": "Worker heartbeats",
            "active_nav": "settings",
            "rows": rows,
            "summary": summary,
            "now_iso": iso(datetime.now(UTC)),
        },
    )


@router.get("/api/heartbeat-alerts.json")
async def heartbeat_alerts_json() -> JSONResponse:
    """Return the alert + table data as JSON for external probes."""
    rows, alerts = await _build_view_rows()
    payload: dict[str, object] = {
        "now": iso(datetime.now(UTC)),
        "workers": rows,
        "alerts": [dict(a) for a in alerts],
    }
    return JSONResponse(payload)


@router.post("/api/heartbeat-alerts/clear-dedupe", response_model=None)
async def heartbeat_alerts_clear_dedupe(request: Request) -> JSONResponse | RedirectResponse:
    """Drop every ``last_alerted_at_*`` row from ``kv_settings``.

    Form-driven callers (the admin page button) get a 303 redirect
    back to ``/admin/heartbeat-alerts`` so the post-redirect-get
    pattern works without JavaScript. Programmatic callers (JSON
    content type) get a JSON body with the row-removal count.
    """
    removed = await clear_dedupe()
    log.info("heartbeat_alerts.clear_dedupe", removed=removed)

    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return JSONResponse({"removed": removed})
    return RedirectResponse(
        url="/admin/heartbeat-alerts",
        status_code=303,
    )


__all__ = ["router"]
