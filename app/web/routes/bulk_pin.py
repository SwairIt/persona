"""HTMX-driven bulk-pin UI backed by :func:`app.bulk_pin.bulk_pin`.

Mirrors the bulk-delete preview/confirm flow from
:mod:`app.web.routes.bulk_delete`, including the HMAC-signed token that
binds a confirm POST to the exact ``query`` + ``matched`` count seen at
preview time — change either and the token mismatches, forcing the user
back through preview. The same posture is intentional: pinning is not
destructive but it does override retention, so an accidental click on a
runaway query (``query="."``) should never lock 10 000 shots in cold-tier
purgatory forever.

Endpoints:

* ``GET  /admin/bulk-pin``         — render the page.
* ``POST /admin/bulk-pin/preview`` — dry-run the query and render a
  fragment with the match count + a fresh HMAC token.
* ``POST /admin/bulk-pin/confirm`` — verify the token and execute the
  real pin.

The page also exposes a sibling un-pin form that posts directly to
``/admin/bulk-pin/unpin`` (no preview step — un-pinning is reversible by
re-pinning, and it never deletes data, so the HMAC step would be empty
ceremony).
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.bulk_pin import bulk_pin, bulk_unpin
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

log = get_logger("persona.web.bulk_pin")

router = APIRouter(tags=["bulk-pin"])

_QUERY_MIN, _QUERY_MAX = 1, 500
_LIMIT_DEFAULT = 100
_LIMIT_MAX = 10_000


# Process-local fallback HMAC secret — only consulted when the install has
# no ``settings.session_secret`` configured. Restarting the server
# invalidates pending previews, which is the desired conservative
# behaviour for an admin tier-mutation endpoint.
_PROCESS_SECRET: bytes = secrets.token_bytes(32)


def _secret() -> bytes:
    """Return the HMAC secret — ``settings.session_secret`` else cached random.

    Matches the lookup pattern used by :mod:`app.web.routes.bulk_delete` so
    the two admin pages share the same secret when one is configured.
    """
    settings = get_settings()
    configured = getattr(settings, "session_secret", None)
    if configured:
        text = str(configured)
        if text:
            return text.encode()
    return _PROCESS_SECRET


def _make_token(query: str, matched: int) -> str:
    """Stable token = HMAC-SHA256(secret, "<query>|<count>").

    Including the matched count means the user cannot replay a token
    captured from a previous, smaller preview against a larger live query.
    """
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


@router.get("/admin/bulk-pin", response_class=HTMLResponse)
async def bulk_pin_page(request: Request) -> HTMLResponse:
    """Render the bulk-pin admin page."""
    return templates.TemplateResponse(
        request,
        "bulk_pin.html",
        {
            "title": "Bulk pin",
            "active_nav": "settings",
            "default_limit": _LIMIT_DEFAULT,
        },
    )


@router.post("/admin/bulk-pin/preview", response_class=HTMLResponse)
async def bulk_pin_preview(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Run the FTS query as a dry-run and return the preview fragment."""
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    result = await bulk_pin(query_v, limit_v, dry_run=True)
    token = _make_token(query_v, result["matched"])

    return templates.TemplateResponse(
        request,
        "_bulk_pin_preview.html",
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


@router.post("/admin/bulk-pin/confirm", response_class=HTMLResponse)
async def bulk_pin_confirm(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
    token: str = Form(...),
    matched: int = Form(...),
) -> HTMLResponse:
    """Verify the HMAC token then execute the pin for real."""
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)
    if matched < 0:
        raise HTTPException(status_code=400, detail="matched must be >= 0")

    expected = _make_token(query_v, matched)
    if not hmac.compare_digest(expected, token):
        log.warning("bulk.pin.bad_token", query=query_v, matched=matched)
        await log_action(
            "bulk_pin.confirm",
            target=query_v,
            detail="token mismatch",
            success=False,
        )
        raise HTTPException(status_code=400, detail="Confirmation token mismatch.")

    result = await bulk_pin(query_v, limit_v, dry_run=False)
    matched_count = int(result["matched"])
    pinned_count = int(result["pinned"])
    await log_action(
        "bulk_pin.confirm",
        target=query_v,
        detail=str(matched_count) + " matched, " + str(pinned_count) + " pinned",
    )

    return templates.TemplateResponse(
        request,
        "_bulk_pin_preview.html",
        {
            "result": {
                "query": query_v,
                "matched": result["matched"],
                "pinned": result["pinned"],
                "mode": "pin",
            },
        },
    )


@router.post("/admin/bulk-pin/unpin", response_class=HTMLResponse)
async def bulk_pin_unpin(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Un-pin every screenshot matching ``query`` — no preview step.

    Un-pinning is non-destructive (the shot drops back to ``hot`` and the
    regular tier sweep handles it from there), so we skip the HMAC dance
    that guards :func:`bulk_pin_confirm`. The result fragment is rendered
    by the same template so the UI stays consistent.
    """
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    result = await bulk_unpin(query_v, limit_v)
    matched_count = int(result["matched"])
    unpinned_count = int(result["pinned"])
    await log_action(
        "bulk_pin.unpin",
        target=query_v,
        detail=str(matched_count) + " matched, " + str(unpinned_count) + " unpinned",
    )

    return templates.TemplateResponse(
        request,
        "_bulk_pin_preview.html",
        {
            "result": {
                "query": query_v,
                "matched": result["matched"],
                "pinned": result["pinned"],
                "mode": "unpin",
            },
        },
    )


__all__ = ["router"]
