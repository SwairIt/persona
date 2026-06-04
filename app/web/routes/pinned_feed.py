"""Personal RSS feed for the most-recent pinned screenshots.

Why this exists
---------------
Persona's pinned-shot surface is HTML-only — ``/pinmap`` lets the
operator browse the moments they've marked as important, but there's
no *passive* way to be notified when a new pin lands. Mirroring the
:mod:`app.web.routes.audit_rss` + ``/feeds/journal.rss`` shape, this
route exposes ``/feeds/pinned.rss`` so a feed reader on the same
machine can poll and surface a desktop notification every time the
operator (or the auto-pin engine) pins something new.

Security contract
-----------------
* **Loopback-only.** A pinned shot's body can mirror anything that was
  on screen at capture time — the same threat surface as the audit
  feed. We reject anything that isn't ``127.0.0.1`` / ``::1`` before
  we touch the DB, and we deliberately do *not* honour
  ``X-Forwarded-For``. If you want remote access, tunnel over SSH.
* **Token-gated under feed_auth_required.** When the operator has
  flipped :attr:`Settings.feed_auth_required` on, the request must
  carry a valid ``?token=…`` whose ``feed_pattern`` covers
  ``/feeds/pinned.rss``. This mirrors ``/feeds/journal.rss`` exactly
  — the same token that already covers ``/feeds/*`` will work here
  without re-issuing.
* **XML-safe.** Every dynamic value (title, app, window-title,
  alt-text, OCR snippet, timestamps, URLs) is XML-escaped inside
  :func:`app.pinned_feed.build_pinned_rss` before it touches the
  response body. OCR text is whatever was on screen at capture time
  — we treat it as untrusted free-form input.
"""

from __future__ import annotations

from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.feed_tokens import verify_token as verify_feed_token
from app.logging_setup import get_logger
from app.pinned_feed import build_pinned_rss
from app.settings import get_settings

router = APIRouter(prefix="/feeds", tags=["feeds"])

log = get_logger("persona.pinned_feed")

# Spec: 50 most-recent pinned shots.
_MAX_RSS_ITEMS = 50


@router.get("/pinned.rss")
async def pinned_rss(request: Request) -> Response:
    """RSS 2.0 feed of the 50 most-recent pinned screenshots.

    Loopback-only — every non-local request is rejected with 403
    before we touch the DB. When :attr:`Settings.feed_auth_required`
    is on, the request must also carry a valid ``?token=…`` whose
    pattern matches ``/feeds/pinned.rss``.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("pinned_feed.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Pinned feed is loopback-only",
        )

    await _enforce_feed_token(request)

    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    body = await build_pinned_rss(host=base, limit=_MAX_RSS_ITEMS)

    log.info("pinned_feed.served", bytes=len(body))
    return Response(
        content=body,
        media_type="application/rss+xml; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    Mirrors :mod:`app.web.routes.audit_rss` — Persona is a
    single-user, local-first tool and the only legitimate consumer of
    the pinned feed is a feed reader on the same machine. We refuse
    to trust ``X-Forwarded-For`` here for the same reason.
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
    ``feed_pattern`` matches ``/feeds/pinned.rss`` via
    :func:`app.feed_tokens.verify_token` — same policy as
    ``/feeds/journal.rss`` so a single token configured with
    ``/feeds/*`` covers both feeds.

    Status codes intentionally mirror :mod:`app.web.routes.rss`:
    missing token → 401; known-but-not-authorised → 403; unknown /
    revoked → 401 (so we can't be probed for token existence).
    """
    settings = get_settings()
    if not settings.feed_auth_required:
        return

    raw = request.query_params.get("token", "").strip()
    if not raw:
        log.info("pinned_feed.gate_missing", path=request.url.path)
        raise HTTPException(status_code=401, detail="Feed token required")

    verdict = await verify_feed_token(raw, request.url.path)
    if not verdict.get("ok"):
        reason = verdict.get("reason", "unknown")
        log.info(
            "pinned_feed.gate_denied",
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
