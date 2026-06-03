"""HTMX-driven bulk add-to-auto-collection UI.

Apply an auto-collection's tag to every screenshot whose FTS5 MATCH on a
query succeeds. By the v0.23 auto-collection contract — membership is
computed on read from ``screenshot_tags`` — tagging the matches makes
them appear at ``/collection/{slug}`` immediately, no further plumbing.

The endpoint reuses :func:`app.bulk_tag.bulk_tag` (v0.24) so the FTS5
SQL, normalisation rules and idempotent ``INSERT OR IGNORE`` semantics
stay in one place. The route only contributes:

* an admin form that lists existing collection rules as a dropdown so
  the operator can never mistype the target tag,
* a dry-run preview that surfaces the match count and a fresh HMAC
  token, and
* an HMAC-gated confirm that performs the real bulk-tag and writes an
  audit row.

Endpoints:

* ``GET  /admin/bulk-collection``         — render the form.
* ``POST /admin/bulk-collection/preview`` — dry-run the query, return a
  fragment with the match count + token.
* ``POST /admin/bulk-collection/apply``   — verify the token then apply
  the collection's tag to every match.

The HMAC token binds ``slug + query + matched`` together, so changing
any of the three between preview and apply invalidates it and forces a
fresh preview — the same posture as :mod:`app.web.routes.bulk_delete`
and :mod:`app.web.routes.bulk_pin`.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.bulk_tag import bulk_tag
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.bulk_collection")

router = APIRouter(tags=["bulk-collection"])

_QUERY_MIN, _QUERY_MAX = 1, 500
_LIMIT_DEFAULT = 100
_LIMIT_MAX = 10_000


# Process-local fallback HMAC secret — only consulted when the install
# has no ``settings.session_secret`` configured. Restarting the server
# invalidates pending previews, which is the conservative default for
# an admin mutation endpoint.
_PROCESS_SECRET: bytes = secrets.token_bytes(32)


def _secret() -> bytes:
    """Return the HMAC secret — ``settings.session_secret`` else cached.

    Matches the lookup pattern used by the sibling bulk-delete and
    bulk-pin admin pages so all three share a single secret when one is
    configured.
    """
    settings = get_settings()
    configured = getattr(settings, "session_secret", None)
    if configured:
        text = str(configured)
        if text:
            return text.encode()
    return _PROCESS_SECRET


def _make_token(slug: str, query: str, matched: int) -> str:
    """Stable token = HMAC-SHA256(secret, "<slug>|<query>|<count>").

    Including ``slug`` in the message means a token captured for one
    collection cannot be replayed against another — even if the query +
    match count happen to coincide.
    """
    message = f"{slug}|{query}|{matched}".encode()
    return hmac.new(_secret(), message, sha256).hexdigest()


def _validate_query(query: str) -> str:
    cleaned = (query or "").strip()
    if not (_QUERY_MIN <= len(cleaned) <= _QUERY_MAX):
        msg = f"query must be {_QUERY_MIN}..{_QUERY_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > _LIMIT_MAX:
        msg = f"limit must be 1..{_LIMIT_MAX}"
        raise HTTPException(status_code=400, detail=msg)
    return limit


async def _list_collections(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all auto-collection rules for the dropdown.

    Mirrors the shape used by :mod:`app.web.routes.auto_collections` so
    the template can pick whichever columns it wants without surprises.
    """
    cursor = await conn.execute(
        "SELECT slug, title, tag, public "
        "FROM auto_collection ORDER BY title ASC, slug ASC"
    )
    rows = await cursor.fetchall()
    return [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "tag": str(row["tag"]),
            "public": int(row["public"]),
        }
        for row in rows
    ]


async def _fetch_tag_for_slug(
    conn: aiosqlite.Connection, slug: str
) -> str | None:
    """Resolve a collection slug to its bound tag, or ``None`` if unknown."""
    cursor = await conn.execute(
        "SELECT tag FROM auto_collection WHERE slug = ?", (slug,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["tag"])


def _validate_slug(slug: str) -> str:
    """Light slug sanity check — full validation happens against the DB."""
    cleaned = (slug or "").strip().lower()
    if not cleaned or len(cleaned) > 40:
        raise HTTPException(status_code=400, detail="slug missing or too long")
    return cleaned


@router.get("/admin/bulk-collection", response_class=HTMLResponse)
async def bulk_collection_page(request: Request) -> HTMLResponse:
    """Render the bulk add-to-collection admin page."""
    async with get_connection() as conn:
        rules = await _list_collections(conn)
    return templates.TemplateResponse(
        request,
        "bulk_collection_add.html",
        {
            "title": "Bulk add to auto-collection",
            "active_nav": "tags",
            "rules": rules,
            "default_limit": _LIMIT_DEFAULT,
        },
    )


@router.post("/admin/bulk-collection/preview", response_class=HTMLResponse)
async def bulk_collection_preview(
    request: Request,
    slug: str = Form(...),
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Dry-run the query against the chosen collection's tag.

    Returns a fragment summarising the match count plus an HMAC token
    binding ``slug + query + matched`` for the confirm step.
    """
    slug_v = _validate_slug(slug)
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    async with get_connection() as conn:
        tag = await _fetch_tag_for_slug(conn, slug_v)
    if tag is None:
        raise HTTPException(status_code=404, detail="Collection not found")

    result = await bulk_tag(tag, query_v, limit_v, dry_run=True)
    matched = int(result["matched"])
    token = _make_token(slug_v, query_v, matched)

    return templates.TemplateResponse(
        request,
        "bulk_collection_add.html",
        {
            "title": "Bulk add to auto-collection",
            "active_nav": "tags",
            "fragment": "preview",
            "preview": {
                "slug": slug_v,
                "tag": tag,
                "query": query_v,
                "limit": limit_v,
                "matched": matched,
                "token": token,
            },
        },
    )


@router.post("/admin/bulk-collection/apply", response_class=HTMLResponse)
async def bulk_collection_apply(
    request: Request,
    slug: str = Form(...),
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
    matched: int = Form(...),
    token: str = Form(...),
) -> HTMLResponse:
    """Verify the HMAC token, then apply the collection's tag to matches."""
    slug_v = _validate_slug(slug)
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)
    if matched < 0:
        raise HTTPException(status_code=400, detail="matched must be >= 0")

    expected = _make_token(slug_v, query_v, matched)
    if not hmac.compare_digest(expected, token):
        log.warning(
            "bulk_collection.bad_token",
            slug=slug_v,
            query=query_v,
            matched=matched,
        )
        await log_action(
            "bulk_collection.apply",
            target=slug_v,
            detail="token mismatch",
            success=False,
        )
        raise HTTPException(status_code=400, detail="Confirmation token mismatch.")

    async with get_connection() as conn:
        tag = await _fetch_tag_for_slug(conn, slug_v)
    if tag is None:
        await log_action(
            "bulk_collection.apply",
            target=slug_v,
            detail="collection vanished between preview and apply",
            success=False,
        )
        raise HTTPException(status_code=404, detail="Collection not found")

    result = await bulk_tag(tag, query_v, limit_v, dry_run=False)
    matched_count = int(result["matched"])
    affected_count = int(result["affected"])

    log.info(
        "bulk_collection.applied",
        slug=slug_v,
        tag=tag,
        query=query_v,
        matched=matched_count,
        affected=affected_count,
    )
    await log_action(
        "bulk_collection.apply",
        target=slug_v,
        detail=(
            f"tag={tag} query={query_v} matched={matched_count} "
            f"affected={affected_count}"
        ),
    )

    return templates.TemplateResponse(
        request,
        "bulk_collection_add.html",
        {
            "title": "Bulk add to auto-collection",
            "active_nav": "tags",
            "fragment": "result",
            "result": {
                "slug": slug_v,
                "tag": tag,
                "query": query_v,
                "matched": matched_count,
                "affected": affected_count,
            },
        },
    )


__all__ = ["router"]
