"""API tokens settings UI — list / create / revoke (v0.34).

``GET  /settings/api-tokens``               renders the management page
``POST /settings/api-tokens``               mints a fresh token; the raw
                                            value is shown in a one-time
                                            banner via a query param
``POST /settings/api-tokens/{id}/revoke``   soft-revokes a token

The raw value is round-tripped through a redirect query parameter so a
browser refresh of the resulting page doesn't accidentally re-issue a
new token. This is a deliberate UX trade-off — the value lives in the
browser's history for that single session and the user is told (loudly,
in the banner) to copy it now and never see it again.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api_tokens import create_token, list_tokens, revoke_token
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.api_tokens")

# Whitelist of scope strings the UI offers. The DB column is free-form
# TEXT so other writers (CLI scripts, future migrations) can store
# anything, but the form here keeps the surface area predictable.
_ALLOWED_SCOPES: tuple[str, ...] = ("read", "read,write")


def _normalise_scopes(raw: str) -> str:
    """Map user input to one of the whitelisted scope strings.

    Anything unrecognised falls back to ``read`` — failing closed is the
    right default for a permissions field.
    """
    candidate = raw.strip().lower().replace(" ", "")
    if candidate in _ALLOWED_SCOPES:
        return candidate
    return "read"


@router.get("/settings/api-tokens", response_class=HTMLResponse)
async def api_tokens_page(request: Request, new_token: str | None = None) -> HTMLResponse:
    """Render the list of issued tokens plus the create form.

    ``new_token`` only ever arrives via the POST→redirect handshake; it
    is *not* a documented API parameter and must never be linked to
    externally. We treat it as plain string with no validation beyond
    "is it present" so the template can decide whether to show the
    one-time banner.
    """
    tokens = await list_tokens()
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "api_tokens.html",
        {
            "title": "API tokens",
            "active_nav": "settings",
            "tokens": tokens,
            "new_token": new_token,
            "allowed_scopes": _ALLOWED_SCOPES,
            "api_auth_required": settings.api_auth_required,
        },
    )


@router.post("/settings/api-tokens")
async def api_tokens_create(
    request: Request,
    name: str = Form(...),
    scopes: str = Form(default="read"),
) -> RedirectResponse:
    """Mint a new token and bounce back with the raw value in the URL.

    The redirect is 303 so refreshing the resulting page is idempotent
    (no second token will be minted). The raw value is deliberately
    *not* stored in any server-side flash — it lives only on the user's
    screen until they navigate away.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        # Bouncing back without a banner is the simplest possible
        # "validation failed" UX; the form keeps its values via the
        # browser's bfcache.
        return RedirectResponse(url="/settings/api-tokens", status_code=303)

    scope_string = _normalise_scopes(scopes)
    raw = await create_token(name=cleaned_name, scopes=scope_string)
    # The raw value is urlsafe by construction, so direct interpolation
    # is fine — no further encoding step required.
    return RedirectResponse(
        url=f"/settings/api-tokens?new_token={raw}",
        status_code=303,
    )


@router.post("/settings/api-tokens/{token_id}/revoke")
async def api_tokens_revoke(token_id: int) -> RedirectResponse:
    """Soft-revoke a token (sets ``revoked_at``) and return to the list."""
    await revoke_token(token_id)
    return RedirectResponse(url="/settings/api-tokens", status_code=303)
