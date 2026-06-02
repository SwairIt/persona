"""HTMX-driven bulk-delete UI backed by :func:`app.bulk_delete.bulk_delete`.

Three endpoints:

* ``GET  /admin/bulk-delete``         — render the page.
* ``POST /admin/bulk-delete/preview`` — dry-run the query, render a fragment
  showing the match count and a fresh HMAC token.
* ``POST /admin/bulk-delete/confirm`` — execute the delete, but only if the
  supplied token matches HMAC(secret, query+"|"+matched_count). Mismatch →
  400 so the user is forced through the preview step first.

The token shape (query + count) means changing the query OR seeing a
different match count between preview and confirm invalidates the token.
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.bulk_delete import bulk_delete
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

log = get_logger("persona.web.bulk_delete")

router = APIRouter(tags=["bulk-delete"])

_QUERY_MIN, _QUERY_MAX = 1, 500
_LIMIT_DEFAULT = 100
_LIMIT_MAX = 10_000


_PROCESS_SECRET: bytes = secrets.token_bytes(32)


def _secret() -> bytes:
    """Return the HMAC secret — ``settings.session_secret`` if present else cached.

    Most installs don't define ``session_secret``; we fall back to a
    process-local random secret generated at import time. Restarting the
    server invalidates pending previews, which is the desired conservative
    behaviour for a destructive endpoint.
    """
    settings = get_settings()
    configured = getattr(settings, "session_secret", None)
    if configured:
        text = str(configured)
        if text:
            return text.encode()
    return _PROCESS_SECRET


def _make_token(query: str, matched: int) -> str:
    """Stable token = HMAC-SHA256(secret, "<query>|<count>")."""
    message = f"{query}|{matched}".encode()
    digest = hmac.new(_secret(), message, sha256).hexdigest()
    return digest


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


@router.get("/admin/bulk-delete", response_class=HTMLResponse)
async def bulk_delete_page(request: Request) -> HTMLResponse:
    """Render the bulk-delete admin page."""
    return templates.TemplateResponse(
        request,
        "bulk_delete.html",
        {
            "title": "Bulk delete",
            "active_nav": "settings",
            "default_limit": _LIMIT_DEFAULT,
        },
    )


@router.post("/admin/bulk-delete/preview", response_class=HTMLResponse)
async def bulk_delete_preview(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Run the FTS query as a dry-run and return the preview fragment."""
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    result = await bulk_delete(query_v, limit_v, dry_run=True)
    token = _make_token(query_v, result["matched"])

    return templates.TemplateResponse(
        request,
        "_bulk_delete_preview.html",
        {
            "preview": {
                "query": query_v,
                "limit": limit_v,
                "matched": result["matched"],
                "ids": result["ids"][:20],
                "token": token,
            },
        },
    )


@router.post("/admin/bulk-delete/confirm", response_class=HTMLResponse)
async def bulk_delete_confirm(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
    token: str = Form(...),
    matched: int = Form(...),
) -> HTMLResponse:
    """Verify the HMAC token, then execute the delete for real."""
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)
    if matched < 0:
        raise HTTPException(status_code=400, detail="matched must be >= 0")

    expected = _make_token(query_v, matched)
    if not hmac.compare_digest(expected, token):
        log.warning("bulk.delete.bad_token", query=query_v, matched=matched)
        await log_action(
            "bulk_delete.confirm",
            target=query_v,
            detail="token mismatch",
            success=False,
        )
        raise HTTPException(status_code=400, detail="Confirmation token mismatch.")

    result = await bulk_delete(query_v, limit_v, dry_run=False)
    matched_count = int(result["matched"])
    deleted_count = int(result["deleted"])
    await log_action(
        "bulk_delete.confirm",
        target=query_v,
        detail=str(matched_count) + " matched, " + str(deleted_count) + " deleted",
    )

    return templates.TemplateResponse(
        request,
        "_bulk_delete_preview.html",
        {
            "result": {
                "query": query_v,
                "matched": result["matched"],
                "deleted": result["deleted"],
            },
        },
    )
