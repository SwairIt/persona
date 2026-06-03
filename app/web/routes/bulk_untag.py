"""HTMX-driven bulk-untag UI backed by :func:`app.bulk_tag.bulk_untag`.

v0.98 ships the inverse of the v0.24 ``bulk_tag`` web flow: instead of
*attaching* a tag to every screenshot whose FTS5 MATCH on ``query``
succeeds, this route *detaches* one. Same admin posture, same
preview/confirm dance, same HMAC-signed confirmation token that binds a
confirm POST to the exact ``(tag, query, matched)`` triple the operator
saw at preview time — change any of them and the token mismatches,
forcing the user back through the preview step.

Endpoints:

* ``GET  /admin/bulk-untag``         — render the page.
* ``POST /admin/bulk-untag/preview`` — dry-run the query and render a
  fragment with the match count + a fresh HMAC token. The dry-run
  re-uses :func:`app.search.search` directly so we never call
  :func:`app.bulk_tag.bulk_untag` itself with side-effects — the
  v0.24 helper has no ``dry_run`` flag, so semantically "dry-run"
  here means "enumerate matching ids without writing".
* ``POST /admin/bulk-untag/confirm`` — verify the HMAC token, then
  execute :func:`app.bulk_tag.bulk_untag` for real.

Audit-logged under ``bulk_untag.confirm`` so an operator can answer
*which tag was stripped off which query, and how many rows did it
touch?* during an incident review.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.bulk_tag import bulk_untag
from app.logging_setup import get_logger
from app.search import search as fts_search
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.bulk_untag")

router = APIRouter(tags=["bulk-untag"])

# Validation bounds mirror the sibling bulk admin pages so the three
# endpoints accept the same shape of input.
_QUERY_MIN, _QUERY_MAX = 1, 500
_TAG_MIN, _TAG_MAX = 1, 60
_LIMIT_DEFAULT = 100
_LIMIT_MAX = 10_000


# Process-local fallback HMAC secret — consulted only when the install
# has no ``settings.session_secret`` configured. Restarting the server
# invalidates pending previews, which is the desired conservative
# behaviour for an admin mutation endpoint.
_PROCESS_SECRET: bytes = secrets.token_bytes(32)


def _secret() -> bytes:
    """Return the HMAC secret — ``settings.session_secret`` else cached random.

    Matches the lookup pattern used by :mod:`app.web.routes.bulk_delete`
    and :mod:`app.web.routes.bulk_pin` so all three admin pages share
    the same secret when one is configured.
    """
    settings = get_settings()
    configured = getattr(settings, "session_secret", None)
    if configured:
        text = str(configured)
        if text:
            return text.encode()
    return _PROCESS_SECRET


def _make_token(tag: str, query: str, matched: int) -> str:
    """Stable token = HMAC-SHA256(secret, "<tag>|<query>|<count>").

    Including the tag name in the digest means swapping the tag between
    preview and confirm invalidates the token, just like swapping the
    query or replaying a stale match count does. Without the tag in the
    digest a stray confirm POST could untag rows for a *different* tag
    than the one the operator just previewed.
    """
    message = f"{tag}|{query}|{matched}".encode()
    digest = hmac.new(_secret(), message, sha256).hexdigest()
    return digest


def _validate_query(query: str) -> str:
    cleaned = (query or "").strip()
    if not (_QUERY_MIN <= len(cleaned) <= _QUERY_MAX):
        msg = f"query must be {_QUERY_MIN}..{_QUERY_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_tag(tag: str) -> str:
    cleaned = (tag or "").strip()
    if not (_TAG_MIN <= len(cleaned) <= _TAG_MAX):
        msg = f"tag must be {_TAG_MIN}..{_TAG_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    # Match the lower-cased normalisation that :func:`app.bulk_tag.bulk_untag`
    # applies so the preview and the eventual write see the same tag row.
    return cleaned.lower()


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > _LIMIT_MAX:
        msg = f"limit must be 1..{_LIMIT_MAX}"
        raise HTTPException(status_code=400, detail=msg)
    return limit


@router.get("/admin/bulk-untag", response_class=HTMLResponse)
async def bulk_untag_page(request: Request) -> HTMLResponse:
    """Render the bulk-untag admin page."""
    return templates.TemplateResponse(
        request,
        "bulk_untag.html",
        {
            "title": "Bulk untag",
            "active_nav": "settings",
            "default_limit": _LIMIT_DEFAULT,
        },
    )


@router.post("/admin/bulk-untag/preview", response_class=HTMLResponse)
async def bulk_untag_preview(
    request: Request,
    tag: str = Form(...),
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Dry-run the query: enumerate matching ids without removing the tag.

    The v0.24 :func:`app.bulk_tag.bulk_untag` helper has no ``dry_run``
    flag — calling it eagerly would actually strip the tag, which is
    exactly what preview must NOT do. We therefore re-run the same FTS5
    search :func:`app.bulk_tag._resolve_matching_ids` uses internally
    (:func:`app.search.search`) so the preview's match count is
    byte-for-byte the same number the confirm step will see.
    """
    tag_v = _validate_tag(tag)
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    async with get_connection() as conn:
        hits = await fts_search(conn, query=query_v, limit=limit_v)
        ids = [int(hit.screenshot_id) for hit in hits]

    matched = len(ids)
    token = _make_token(tag_v, query_v, matched)

    log.info(
        "bulk_untag.preview",
        tag=tag_v,
        query=query_v,
        limit=limit_v,
        matched=matched,
    )

    return templates.TemplateResponse(
        request,
        "_bulk_untag_preview.html",
        {
            "preview": {
                "tag": tag_v,
                "query": query_v,
                "limit": limit_v,
                "matched": matched,
                "ids": ids[:20],
                "token": token,
            },
        },
    )


@router.post("/admin/bulk-untag/confirm", response_class=HTMLResponse)
async def bulk_untag_confirm(
    request: Request,
    tag: str = Form(...),
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
    token: str = Form(...),
    matched: int = Form(...),
) -> HTMLResponse:
    """Verify the HMAC token, then strip the tag for real.

    The token binds the confirm POST to the exact ``(tag, query, count)``
    triple the preview returned. Any drift — tag swap, query edit, or a
    fresh row appearing/disappearing under the same query — makes the
    HMAC mismatch, forcing another trip through preview.
    """
    tag_v = _validate_tag(tag)
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)
    if matched < 0:
        raise HTTPException(status_code=400, detail="matched must be >= 0")

    expected = _make_token(tag_v, query_v, matched)
    if not hmac.compare_digest(expected, token):
        log.warning(
            "bulk_untag.bad_token",
            tag=tag_v,
            query=query_v,
            matched=matched,
        )
        await log_action(
            "bulk_untag.confirm",
            target=tag_v,
            detail=f"token mismatch query={query_v} matched={matched}",
            success=False,
        )
        raise HTTPException(status_code=400, detail="Confirmation token mismatch.")

    result = await bulk_untag(tag_v, query_v, limit_v)
    matched_count = int(result["matched"])
    affected_count = int(result["affected"])

    log.info(
        "bulk_untag.confirm",
        tag=tag_v,
        query=query_v,
        limit=limit_v,
        matched=matched_count,
        affected=affected_count,
    )
    await log_action(
        "bulk_untag.confirm",
        target=tag_v,
        detail=(
            f"query={query_v} limit={limit_v} "
            f"matched={matched_count} affected={affected_count}"
        ),
    )

    return templates.TemplateResponse(
        request,
        "_bulk_untag_preview.html",
        {
            "result": {
                "tag": tag_v,
                "query": query_v,
                "matched": matched_count,
                "affected": affected_count,
            },
        },
    )


__all__ = ["router"]
