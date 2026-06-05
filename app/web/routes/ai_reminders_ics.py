"""HTTP routes for the AI-reminders iCalendar subscription (v1.53).

Two endpoints:

* ``GET /feeds/reminders.ics`` — a long-lived ``text/calendar``
  subscription that surfaces every pending AI reminder with a
  ``due_at`` value. Calendar clients (Apple Calendar, Google Calendar,
  Outlook) poll on a schedule of their own; dismissals and snoozes
  applied via :mod:`app.web.routes.ai_reminders` propagate to the
  feed without the user lifting a finger.
* ``GET /feeds/reminders/subscribe-help`` — HTML instructions for
  pasting the feed URL into each of the three big calendar clients.

Security contract mirrors :mod:`app.web.routes.tag_feed`:

* The request must originate from ``127.0.0.1`` / ``::1`` (single-user,
  local-first tool — same rationale as the rest of the feed family).
* When :attr:`Settings.feed_auth_required` is on, the request must
  *also* carry a valid ``?token=…`` whose ``feed_pattern`` matches
  ``/feeds/reminders.ics``. A token issued for ``/feeds/*`` already
  covers this path so power users don't have to re-issue.
* We deliberately do *not* honour ``X-Forwarded-For`` — remote access
  is by SSH tunnel.

The ``Content-Disposition: inline`` header (rather than ``attachment``)
matters for calendar subscriptions: Apple Calendar's ``webcal://``
handler refuses to follow an ``attachment`` response, and Google
Calendar's URL-import flow silently downgrades to "download" mode.
"""

from __future__ import annotations

from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.ai_reminders_ics import build_reminders_ics
from app.feed_tokens import verify_token as verify_feed_token
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

router = APIRouter(tags=["ai-reminders-ics"])

log = get_logger("persona.web.ai_reminders_ics")

# The feed filename the browser sees in the "Save as" dialog when the
# user clicks the link directly. Kept short and dash-separated so it
# survives shells that don't quote spaces.
_ICS_FILENAME = "persona-reminders.ics"


@router.get("/feeds/reminders.ics", response_model=None)
async def reminders_ics_feed(request: Request) -> Response:
    """Return the AI-reminders ICS subscription document.

    Loopback-only by default — every non-local request is rejected
    with 403 before we touch the DB. When
    :attr:`Settings.feed_auth_required` is on, the request must also
    carry a valid ``?token=…`` whose pattern matches
    ``/feeds/reminders.ics``.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("ai_reminders_ics.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="AI reminders feed is loopback-only",
        )

    await _enforce_feed_token(request)

    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    try:
        body = await build_reminders_ics(host=base)
    except Exception:
        log.exception("ai_reminders_ics.route.failed")
        raise HTTPException(
            status_code=500,
            detail="AI reminders ICS export failed",
        ) from None

    payload = body.encode("utf-8")
    log.info("ai_reminders_ics.route.ok", bytes=len(payload))

    return Response(
        content=payload,
        media_type="text/calendar; charset=utf-8",
        headers={
            # Inline so webcal:// + Google's "from URL" flow recognise
            # the response as a live calendar instead of a one-shot
            # download. The filename hint is honoured by browsers when
            # the user does click "Save as" anyway.
            "Content-Disposition": f'inline; filename="{_ICS_FILENAME}"',
            "Content-Length": str(len(payload)),
            # External calendar clients poll on their own cadence; let
            # them decide. ``no-store`` would defeat the conditional-GET
            # optimisations Google Calendar relies on, so we just
            # disable shared-cache reuse.
            "Cache-Control": "private, max-age=0",
        },
    )


@router.get("/feeds/reminders/subscribe-help", response_class=HTMLResponse)
async def reminders_subscribe_help(request: Request) -> HTMLResponse:
    """Render the subscribe-instructions page for Apple/Google/Outlook."""
    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"
    feed_url = f"{base}/feeds/reminders.ics"
    # ``webcal://`` is the secret handshake that triggers Apple
    # Calendar's "Subscribe to calendar" flow on click; we strip the
    # scheme prefix from whatever ``settings.host:port`` gave us so the
    # result is a clean ``webcal://host:port/feeds/...`` URL.
    webcal_url = f"webcal://{settings.host}:{settings.port}/feeds/reminders.ics"
    log.info("ai_reminders_ics.help_page")
    return templates.TemplateResponse(
        request,
        "ai_reminders_subscribe.html",
        {
            "title": "Подписка на AI напоминания",
            "active_nav": "reminders",
            "feed_url": feed_url,
            "webcal_url": webcal_url,
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers — mirror :mod:`app.web.routes.tag_feed`.
# ---------------------------------------------------------------------------


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    Mirrors :mod:`app.web.routes.tag_feed`. We refuse to trust
    ``X-Forwarded-For`` for the same reason — Persona is a single-user
    local-first tool, and the only legitimate consumer of the feed is
    a calendar client running on the same machine (or an SSH tunnel
    that terminates on loopback).
    """
    client = request.client
    if client is None:
        return False
    try:
        return ip_address(client.host).is_loopback
    except ValueError:
        return False


async def _enforce_feed_token(request: Request) -> None:
    """Reject the request unless a valid ``?token=…`` covers the path.

    No-op when :attr:`Settings.feed_auth_required` is ``False``. When
    enforcement is on, the request must carry a token whose
    ``feed_pattern`` matches the request path via
    :func:`app.feed_tokens.verify_token` — same policy as the rest of
    the ``/feeds/*`` family so a single token configured with
    ``/feeds/*`` covers every feed in one go.

    Status codes mirror :mod:`app.web.routes.tag_feed`:
    missing token → 401; known-but-not-authorised → 403; unknown /
    revoked → 401 (so we can't be probed for token existence).
    """
    settings = get_settings()
    if not settings.feed_auth_required:
        return

    raw = request.query_params.get("token", "").strip()
    if not raw:
        log.info("ai_reminders_ics.gate_missing", path=request.url.path)
        raise HTTPException(status_code=401, detail="Feed token required")

    verdict = await verify_feed_token(raw, request.url.path)
    if not verdict.get("ok"):
        reason = verdict.get("reason", "unknown")
        log.info(
            "ai_reminders_ics.gate_denied",
            path=request.url.path,
            reason=reason,
        )
        if reason == "unknown":
            raise HTTPException(status_code=401, detail="Invalid feed token")
        raise HTTPException(
            status_code=403,
            detail="Feed token not authorised for this path",
        )


__all__ = ["router"]
