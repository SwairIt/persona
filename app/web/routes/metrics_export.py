"""Prometheus scrape endpoint at ``/metrics``.

Why this exists
---------------
Persona is local-first and single-user, but power-users still want it
to slot into the same monitoring stack (Prometheus pull + Grafana +
Alertmanager) they already run for the rest of their box. The data is
already in the DB; this route just exposes it in the standard wire
format so a stock ``prometheus.yml`` scrape config can target it.

Security contract
-----------------
* **Loopback-only.** ``/metrics`` leaks operational signal a hostile
  observer could use to fingerprint the install (lifetime capture
  counts, worker liveness, storage usage). Same reasoning as
  :mod:`app.web.routes.audit_rss`: refuse any client whose IP is not a
  loopback address. ``X-Forwarded-For`` is *not* honoured — if you
  want remote access, tunnel over SSH.
* **No DB writes.** Every read goes through
  :func:`app.metrics_export.build_metrics_text`; the route itself does
  not touch SQL directly, so there is no SQL surface area to audit
  here.
* **Stable content type.** The response uses the exact media type the
  Prometheus text-format 0.0.4 spec mandates so scrapers do not have to
  fall back to autodetection.
"""

from __future__ import annotations

from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.logging_setup import get_logger
from app.metrics_export import build_metrics_text

router = APIRouter(tags=["metrics"])

log = get_logger("persona.metrics_export.route")

# Exact media type from the Prometheus text-format 0.0.4 spec. Scrapers
# look for this header to confirm wire format; deviating triggers a
# fallback parse path that is slower and may log a warning.
_PROMETHEUS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics")
async def metrics(request: Request) -> PlainTextResponse:
    """Render the Prometheus scrape payload.

    Loopback-only — every non-local request is rejected with 403 before
    we touch the DB, matching the audit-RSS feed's posture so a
    misconfigured reverse proxy cannot accidentally publish operational
    signal to the open internet.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("metrics_export.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Metrics endpoint is loopback-only",
        )

    body = await build_metrics_text()
    return PlainTextResponse(content=body, media_type=_PROMETHEUS_MEDIA_TYPE)


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    Mirrors :func:`app.web.routes.audit_rss._is_loopback_client`. We
    deliberately ignore ``X-Forwarded-For`` — the metrics scraper is
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
