"""Prometheus scrape endpoint at ``/metrics/extended`` (v1.42).

Why this exists
---------------
The v1.41 ``/metrics`` endpoint (:mod:`app.web.routes.metrics_export`)
covers the headline counters / gauges a stock dashboard needs. This
route is the *deeper-signal* sibling — same wire format, same security
posture, but the response body adds:

* per-worker job count (``worker_heartbeat.ticks``);
* per-worker last-error age (``audit_log`` failures);
* lifetime LLM token cost (USD, from ``llm_usage``);
* OCR queue depth (``screenshots.ocr_status = 'pending'``);
* smart-pin pending-suggestion count;
* today's capture-session count.

Splitting into a parallel endpoint keeps the v1.41 contract frozen:
a misbehaving extended query (slow ``audit_log`` scan on a years-old
DB, e.g.) cannot break the Prometheus scrape an operator already runs.

Security contract
-----------------
* **Loopback-only.** Same threat model as v1.41 ``/metrics`` — these
  metrics leak operational signal that fingerprints the install
  (lifetime LLM spend, worker error history). Refuse any client whose
  IP is not a loopback address. ``X-Forwarded-For`` is *not* honoured.
* **No DB writes.** Every read goes through
  :func:`app.metrics_extended.build_extended_metrics_text`; the route
  itself does not touch SQL.
* **Stable content type.** Reuses the exact Prometheus text-format
  0.0.4 media type so scrapers do not have to fall back to
  autodetection.
"""

from __future__ import annotations

from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.logging_setup import get_logger
from app.metrics_extended import build_extended_metrics_text

router = APIRouter(tags=["metrics"])

log = get_logger("persona.metrics_extended.route")

# Identical to the v1.41 route — Prometheus text-format 0.0.4.
_PROMETHEUS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics/extended")
async def metrics_extended(request: Request) -> PlainTextResponse:
    """Render the extended Prometheus scrape payload.

    Loopback-only — every non-local request is rejected with 403 before
    we touch the DB, matching the v1.41 ``/metrics`` posture so a
    misconfigured reverse proxy cannot accidentally publish operational
    signal to the open internet.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("metrics_extended.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Metrics endpoint is loopback-only",
        )

    body = await build_extended_metrics_text()
    return PlainTextResponse(content=body, media_type=_PROMETHEUS_MEDIA_TYPE)


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    Mirrors :func:`app.web.routes.metrics_export._is_loopback_client`.
    We deliberately ignore ``X-Forwarded-For`` — the metrics scraper is
    expected to run on the same machine.
    """
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


__all__ = ["router"]
