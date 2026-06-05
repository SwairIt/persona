"""RSS feed of the auto-generated changelog page (``/feeds/changelog.rss``).

Why this exists
---------------
Persona's ``/changelog`` page already lists the last 200 commits with
``?kind=feat``-style filter chips. v1.57 adds a *passive* surface so a
feed reader can poll ``/feeds/changelog.rss`` and surface a notification
every time a new commit lands — mirroring the ``/feeds/pinned.rss`` /
``/feeds/journal.rss`` shape.

Security contract
-----------------
* **Loopback-only OR token-gated.** Mirrors :mod:`app.web.routes.pinned_feed`
  byte-for-byte: a request that originates from ``127.0.0.1`` / ``::1``
  is allowed through; everything else must carry a valid ``?token=…``
  whose ``feed_pattern`` covers ``/feeds/changelog.rss``. The commit
  history isn't *secret* (it's already on GitHub) but the same
  single-user threat model that governs the other feeds applies — we
  don't expose the host's IP to drive-by RSS crawlers.
* **No ``X-Forwarded-For`` trust.** Same reason as the pinned feed —
  Persona is a local-first tool, the only legitimate consumer is a
  reader on the same machine. If you want remote access, tunnel over
  SSH or mint a feed token.
* **Kind filter is whitelisted.** The optional ``?kind=feat`` query
  parameter is normalised against the same :data:`_KNOWN_KINDS` set
  the HTML route uses — anything unknown collapses to "no filter" so
  a stale URL can't 404 a polling reader.
* **Graceful when git is unavailable.** :class:`GitUnavailableError`
  surfaces as a 404 with a structured ``detail`` rather than a 500 —
  same posture as ``/api/changelog.json``.
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from app.changelog import GitUnavailableError
from app.changelog_rss import build_changelog_rss
from app.feed_tokens import verify_token as verify_feed_token
from app.logging_setup import get_logger
from app.settings import get_settings

router = APIRouter(prefix="/feeds", tags=["feeds"])

log = get_logger("persona.changelog_rss.routes")

# Mirror :data:`app.web.routes.changelog._KNOWN_KINDS` exactly — the
# RSS feed exposes the same filter shape as ``/changelog?kind=feat``.
# Kept as a local constant rather than imported from the HTML route so
# the two surfaces stay independent at the import level (the HTML
# route is allowed to evolve its template-only helpers without
# rippling into this feed module).
_KNOWN_KINDS: Final[frozenset[str]] = frozenset(
    {"feat", "fix", "refactor", "docs", "test", "chore", "other"}
)

# Spec: 100 most-recent commits — twice the pinned-feed item budget
# because commits land far more often than pins, and a reader catching
# up after a long absence still wants reasonable backfill.
_MAX_RSS_ITEMS: Final[int] = 100


@router.get("/changelog.rss")
async def changelog_rss(request: Request, kind: str | None = None) -> Response:
    """RSS 2.0 feed of the most-recent commits from this repo.

    Loopback-only when :attr:`Settings.feed_auth_required` is off; with
    enforcement on, the request must carry a valid ``?token=…`` whose
    pattern matches ``/feeds/changelog.rss``. Optional ``?kind=feat``
    narrows the items to a single conventional-commit bucket.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("changelog_rss.forbidden", client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Changelog feed is loopback-only",
        )

    await _enforce_feed_token(request)

    selected_kind = _normalise_kind(kind)

    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    try:
        body = await build_changelog_rss(
            host=base,
            limit=_MAX_RSS_ITEMS,
            kind=selected_kind,
        )
    except GitUnavailableError as exc:
        log.warning("changelog_rss.unavailable", error=str(exc))
        raise HTTPException(
            status_code=404,
            detail=f"Changelog unavailable: {exc}",
        ) from exc

    log.info(
        "changelog_rss.served",
        bytes=len(body),
        kind=selected_kind,
    )
    return Response(
        content=body,
        media_type="application/rss+xml; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_loopback_client(request: Request) -> bool:
    """True when the request originates from ``127.0.0.1`` / ``::1``.

    Mirrors :mod:`app.web.routes.pinned_feed` — Persona is a
    single-user, local-first tool and the only legitimate consumer of
    the changelog feed is a feed reader on the same machine. We refuse
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
    ``feed_pattern`` matches ``/feeds/changelog.rss`` via
    :func:`app.feed_tokens.verify_token` — same policy as
    ``/feeds/pinned.rss`` so a single token configured with
    ``/feeds/*`` covers both feeds.

    Status codes intentionally mirror :mod:`app.web.routes.pinned_feed`:
    missing token → 401; known-but-not-authorised → 403; unknown /
    revoked → 401 (so the endpoint can't be probed for token existence).
    """
    settings = get_settings()
    if not settings.feed_auth_required:
        return

    raw = request.query_params.get("token", "").strip()
    if not raw:
        log.info("changelog_rss.gate_missing", path=request.url.path)
        raise HTTPException(status_code=401, detail="Feed token required")

    verdict = await verify_feed_token(raw, request.url.path)
    if not verdict.get("ok"):
        reason = verdict.get("reason", "unknown")
        log.info(
            "changelog_rss.gate_denied",
            path=request.url.path,
            reason=reason,
        )
        if reason == "unknown":
            raise HTTPException(status_code=401, detail="Invalid feed token")
        raise HTTPException(
            status_code=403,
            detail="Feed token not authorised for this path",
        )


def _normalise_kind(raw: str | None) -> str | None:
    """Return ``kind`` if it's a known bucket, else ``None``.

    Whitespace and case are normalised so ``?kind=FEAT`` and
    ``?kind=feat `` both resolve cleanly — mirror of the HTML route's
    helper so the two surfaces accept the same inputs.
    """
    if raw is None:
        return None
    token = raw.strip().lower()
    if not token:
        return None
    if token in _KNOWN_KINDS:
        return token
    return None


__all__ = ["router"]
