"""Per-tag RSS feed at ``/feeds/tag/{tag}.rss`` plus the discovery page.

Why this exists
---------------
Persona has tag-aware HTML surfaces (``/tags``, ``/tags/{id}``,
``/tag-gallery``) and a per-tag RSS family at ``/feeds/tags/{name}.rss``
that lives in :mod:`app.web.routes.rss`. This module ships a leaner,
first-class sibling — ``/feeds/tag/{tag}.rss`` — modelled on
``/feeds/pinned.rss`` so the auth wrapper, the structlog event names
and the OPML entry all line up with the rest of the dedicated-feed
family. A power user can paste exactly one URL into NetNewsWire and
watch every shot they tag with ``#work-recipe`` flow into the reader.

Security contract
-----------------
* **Loopback-only OR token-gated under feed_auth_required.** Mirrors
  ``/feeds/pinned.rss``: when :attr:`Settings.feed_auth_required` is
  on, the request must carry a valid ``?token=…`` whose
  ``feed_pattern`` matches ``/feeds/tag/{tag}.rss``. Otherwise the
  request must originate from ``127.0.0.1`` / ``::1``. We deliberately
  do *not* honour ``X-Forwarded-For`` — if you want remote access,
  tunnel over SSH.
* **404 on empty tag.** When the tag has zero matching rows we return
  404 rather than an empty channel; feed readers stop polling dead
  tags instead of silently subscribing to nothing. The catalog page
  ``/feeds/tag/all`` shows only tags with ``count > 0`` so the
  primary discovery path never offers a dead URL.
* **XML-safe.** Every dynamic value (tag, title, app, window-title,
  alt-text, OCR snippet, timestamps, URLs) is XML-escaped inside
  :func:`app.tag_feed.build_tag_rss` before it touches the response
  body.
"""

from __future__ import annotations

from ipaddress import ip_address
from typing import TypedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.feed_tokens import verify_token as verify_feed_token
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.tag_feed import build_tag_rss
from app.web.templates_engine import templates

router = APIRouter(tags=["feeds"])

log = get_logger("persona.tag_feed.route")

# Spec: 50 most-recent shots per tag.
_MAX_RSS_ITEMS = 50


class _TagRow(TypedDict):
    """One row in the discovery page — tag name, count and feed URL."""

    name: str
    count: int
    rss_url: str
    tag_url: str


@router.get("/feeds/tag/all", response_class=HTMLResponse)
async def tag_feeds_index(request: Request) -> HTMLResponse:
    """Render the discovery page that lists every tag with an RSS feed.

    Read-only catalog query against ``tags`` LEFT JOIN ``screenshot_tags``
    so every tag with at least one tagged shot shows up exactly once,
    ordered by usage count. No XML here — this page only renders links
    to the per-tag feeds; each feed enforces its own gate.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT t.name AS name, COUNT(st.screenshot_id) AS n "
            "FROM tags t "
            "LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
            "GROUP BY t.id, t.name "
            "HAVING n > 0 "
            "ORDER BY n DESC, t.name ASC"
        )
        rows = await cursor.fetchall()

    tag_rows: list[_TagRow] = []
    for row in rows:
        name = str(row["name"])
        count = int(row["n"])
        tag_rows.append(
            _TagRow(
                name=name,
                count=count,
                rss_url=f"/feeds/tag/{name}.rss",
                tag_url=f"/tag/{name}",
            )
        )

    log.info("tag_feeds_index.rendered", tags=len(tag_rows))

    return templates.TemplateResponse(
        request,
        "tag_feeds_index.html",
        {
            "title": "Per-tag RSS feeds",
            "active_nav": "settings",
            "tags": tag_rows,
        },
    )


@router.get("/feeds/tag/{tag}.rss")
async def tag_rss(request: Request, tag: str) -> Response:
    """RSS 2.0 feed of the most-recent shots carrying ``#tag``.

    Loopback-only by default — every non-local request is rejected
    with 403 before we touch the DB. When
    :attr:`Settings.feed_auth_required` is on, the request must also
    carry a valid ``?token=…`` whose pattern matches
    ``/feeds/tag/{tag}.rss``. Returns 404 when the tag has zero
    matching shots so feed readers stop polling dead tags.
    """
    if not _is_loopback_client(request):
        client_host = request.client.host if request.client else None
        log.warning("tag_feed.forbidden", tag=tag, client=client_host)
        raise HTTPException(
            status_code=403,
            detail="Per-tag feed is loopback-only",
        )

    await _enforce_feed_token(request)

    settings = get_settings()
    base = f"http://{settings.host}:{settings.port}"

    body = await build_tag_rss(tag=tag, host=base, limit=_MAX_RSS_ITEMS)
    if not body:
        log.info("tag_feed.not_found", tag=tag)
        raise HTTPException(status_code=404, detail=f"Tag has no shots: {tag}")

    log.info("tag_feed.served", tag=tag, bytes=len(body))
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
    the per-tag feed is a feed reader on the same machine. We refuse
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
    ``feed_pattern`` matches the request path via
    :func:`app.feed_tokens.verify_token` — same policy as
    ``/feeds/pinned.rss`` so a single token configured with
    ``/feeds/*`` covers every feed in one go.

    Status codes mirror :mod:`app.web.routes.rss`:
    missing token → 401; known-but-not-authorised → 403; unknown /
    revoked → 401 (so we can't be probed for token existence).
    """
    settings = get_settings()
    if not settings.feed_auth_required:
        return

    raw = request.query_params.get("token", "").strip()
    if not raw:
        log.info("tag_feed.gate_missing", path=request.url.path)
        raise HTTPException(status_code=401, detail="Feed token required")

    verdict = await verify_feed_token(raw, request.url.path)
    if not verdict.get("ok"):
        reason = verdict.get("reason", "unknown")
        log.info(
            "tag_feed.gate_denied",
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
