"""Feed-token settings UI — list / create / revoke (v0.85).

``GET  /settings/feed-tokens``                renders the management page
``POST /settings/feed-tokens``                mints a fresh token; the raw
                                              value is shown in a one-time
                                              banner via a query param
``POST /settings/feed-tokens/{id}/revoke``    soft-revokes a token

The raw value is round-tripped through a redirect query parameter so a
browser refresh of the resulting page doesn't accidentally re-issue a
new token. Same UX pattern as :mod:`app.web.routes.api_tokens`. The
banner exists only on the user's screen until they navigate away;
nothing about the raw string is ever written to structlog, the audit
trail, or any flash store.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.feed_tokens import create_token, list_tokens, revoke_token
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.feed_tokens")


@router.get("/settings/feed-tokens", response_class=HTMLResponse)
async def feed_tokens_page(request: Request, new_token: str | None = None) -> HTMLResponse:
    """Render the list of issued feed tokens plus the create form.

    ``new_token`` only ever arrives via the POST→redirect handshake; it
    is *not* a documented API parameter and must never be linked to
    externally. We treat it as a plain string with no validation beyond
    "is it present" so the template can decide whether to show the
    one-time banner.
    """
    tokens = await list_tokens()
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "feed_tokens.html",
        {
            "title": "Feed tokens",
            "active_nav": "settings",
            "tokens": tokens,
            "new_token": new_token,
            "feed_auth_required": settings.feed_auth_required,
        },
    )


@router.post("/settings/feed-tokens")
async def feed_tokens_create(
    request: Request,
    name: str = Form(...),
    feed_pattern: str = Form(...),
) -> RedirectResponse:
    """Mint a new feed token and bounce back with the raw value in the URL.

    The redirect is 303 so refreshing the resulting page is idempotent
    (no second token will be minted). The raw value is deliberately
    *not* stored in any server-side flash — it lives only on the
    user's screen until they navigate away.

    Both ``name`` and ``feed_pattern`` are required; an empty value in
    either field bounces back to the list with an audit row recording
    the rejection so the operator can spot misfires.
    """
    cleaned_name = name.strip()
    cleaned_pattern = feed_pattern.strip()
    if not cleaned_name or not cleaned_pattern:
        # Bouncing back without a banner is the simplest possible
        # "validation failed" UX; the form keeps its values via the
        # browser's bfcache.
        await log_action(
            "feed_token.create",
            target=cleaned_name,
            detail=(
                "empty name rejected"
                if not cleaned_name
                else "empty feed_pattern rejected"
            ),
            success=False,
        )
        return RedirectResponse(url="/settings/feed-tokens", status_code=303)

    raw = await create_token(name=cleaned_name, feed_pattern=cleaned_pattern)
    # Never log the raw token value — only the name + pattern survive
    # in the audit trail. The raw string lives on the user's screen
    # for one render and is then gone.
    await log_action(
        "feed_token.create",
        target=cleaned_name,
        detail="feed_pattern=" + cleaned_pattern,
    )
    # The raw value is urlsafe by construction, so direct interpolation
    # is fine — no further encoding step required.
    return RedirectResponse(
        url=f"/settings/feed-tokens?new_token={raw}",
        status_code=303,
    )


@router.post("/settings/feed-tokens/{token_id}/revoke")
async def feed_tokens_revoke(token_id: int) -> RedirectResponse:
    """Soft-revoke a feed token (sets ``revoked_at``) and return to the list."""
    changed = await revoke_token(token_id)
    await log_action(
        "feed_token.revoke",
        target=str(token_id),
        detail="already revoked or unknown" if not changed else None,
        success=bool(changed),
    )
    return RedirectResponse(url="/settings/feed-tokens", status_code=303)
