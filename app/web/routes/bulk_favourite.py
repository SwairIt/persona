"""HTMX-driven bulk-favourite UI backed by :func:`app.bulk_favourite.bulk_favourite`.

Mirrors the preview/confirm flow from :mod:`app.web.routes.bulk_pin`,
including the HMAC-signed token that binds a confirm POST to the exact
``query`` + ``matched`` count seen at preview time — change either and
the token mismatches, forcing the user back through preview. Favouriting
is non-destructive (the only side-effect is a one-row insert into the
``favourite`` table) but the same posture is intentional: a runaway
query (``query="."``) on a 50k-shot install should not silently star the
entire memory and turn ``/favourites`` into a noise-grid the user can no
longer navigate.

Endpoints:

* ``GET  /admin/bulk-favourite``         — render the page.
* ``POST /admin/bulk-favourite/preview`` — dry-run the query and render
  a fragment with the match count + a fresh HMAC token.
* ``POST /admin/bulk-favourite/confirm`` — verify the token and execute
  the real favourite.

The page also exposes a sibling un-favourite form that posts directly to
``/admin/bulk-favourite/unfavourite`` (no preview step — un-favouriting
is reversible by re-favouriting and it never deletes a screenshot, so
the HMAC step would be empty ceremony — same call as bulk-unpin).
"""

from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.audit import log_action
from app.bulk_favourite import bulk_favourite, bulk_unfavourite
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

log = get_logger("persona.web.bulk_favourite")

router = APIRouter(tags=["bulk-favourite"])

# Validation bounds mirror the sibling bulk admin pages so the three
# endpoints accept the same shape of input.
_QUERY_MIN, _QUERY_MAX = 1, 500
_LIMIT_DEFAULT = 100
_LIMIT_MAX = 10_000


# Process-local fallback HMAC secret — only consulted when the install
# has no ``settings.session_secret`` configured. Restarting the server
# invalidates pending previews, which is the desired conservative
# behaviour for an admin mutation endpoint.
_PROCESS_SECRET: bytes = secrets.token_bytes(32)


def _secret() -> bytes:
    """Return the HMAC secret — ``settings.session_secret`` else cached random.

    Matches the lookup pattern used by :mod:`app.web.routes.bulk_pin` and
    :mod:`app.web.routes.bulk_delete` so all bulk admin pages share the
    same secret when one is configured.
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
    captured from a previous, smaller preview against a larger live
    query.
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


@router.get("/admin/bulk-favourite", response_class=HTMLResponse)
async def bulk_favourite_page(request: Request) -> HTMLResponse:
    """Render the bulk-favourite admin page."""
    return templates.TemplateResponse(
        request,
        "bulk_favourite.html",
        {
            "title": "Bulk favourite",
            "active_nav": "settings",
            "default_limit": _LIMIT_DEFAULT,
        },
    )


@router.post("/admin/bulk-favourite/preview", response_class=HTMLResponse)
async def bulk_favourite_preview(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Run the FTS query as a dry-run and return the preview fragment."""
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    result = await bulk_favourite(query_v, limit_v, dry_run=True)
    token = _make_token(query_v, result["matched"])

    log.info(
        "bulk_favourite.preview",
        query=query_v,
        limit=limit_v,
        matched=result["matched"],
    )

    return templates.TemplateResponse(
        request,
        "_bulk_favourite_preview.html",
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


@router.post("/admin/bulk-favourite/confirm", response_class=HTMLResponse)
async def bulk_favourite_confirm(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
    token: str = Form(...),
    matched: int = Form(...),
) -> HTMLResponse:
    """Verify the HMAC token then execute the favourite for real."""
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)
    if matched < 0:
        raise HTTPException(status_code=400, detail="matched must be >= 0")

    expected = _make_token(query_v, matched)
    if not hmac.compare_digest(expected, token):
        log.warning("bulk_favourite.bad_token", query=query_v, matched=matched)
        await log_action(
            "bulk_favourite.confirm",
            target=query_v,
            detail="token mismatch",
            success=False,
        )
        raise HTTPException(status_code=400, detail="Confirmation token mismatch.")

    result = await bulk_favourite(query_v, limit_v, dry_run=False)
    matched_count = int(result["matched"])
    favourited_count = int(result["favourited"])

    log.info(
        "bulk_favourite.confirm",
        query=query_v,
        limit=limit_v,
        matched=matched_count,
        favourited=favourited_count,
    )
    await log_action(
        "bulk_favourite.confirm",
        target=query_v,
        detail=(
            f"limit={limit_v} matched={matched_count} "
            f"favourited={favourited_count}"
        ),
    )

    return templates.TemplateResponse(
        request,
        "_bulk_favourite_preview.html",
        {
            "result": {
                "query": query_v,
                "matched": matched_count,
                "favourited": favourited_count,
                "mode": "favourite",
            },
        },
    )


@router.post("/admin/bulk-favourite/unfavourite", response_class=HTMLResponse)
async def bulk_favourite_unfavourite(
    request: Request,
    query: str = Form(...),
    limit: int = Form(_LIMIT_DEFAULT),
) -> HTMLResponse:
    """Un-favourite every screenshot matching ``query`` — no preview step.

    Un-favouriting only removes a discovery shortcut (the screenshot row
    itself is untouched), so we skip the HMAC dance that guards
    :func:`bulk_favourite_confirm`. The result fragment is rendered by
    the same template so the UI stays consistent — same posture as
    :func:`app.web.routes.bulk_pin.bulk_pin_unpin`.
    """
    query_v = _validate_query(query)
    limit_v = _validate_limit(limit)

    result = await bulk_unfavourite(query_v, limit_v)
    matched_count = int(result["matched"])
    unfavourited_count = int(result["favourited"])

    log.info(
        "bulk_favourite.unfavourite",
        query=query_v,
        limit=limit_v,
        matched=matched_count,
        favourited=unfavourited_count,
    )
    await log_action(
        "bulk_favourite.unfavourite",
        target=query_v,
        detail=(
            f"limit={limit_v} matched={matched_count} "
            f"unfavourited={unfavourited_count}"
        ),
    )

    return templates.TemplateResponse(
        request,
        "_bulk_favourite_preview.html",
        {
            "result": {
                "query": query_v,
                "matched": matched_count,
                "favourited": unfavourited_count,
                "mode": "unfavourite",
            },
        },
    )


__all__ = ["router"]
