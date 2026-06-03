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

from app.api_tokens import ALLOWED_SCOPES, create_token, list_tokens, revoke_token
from app.audit import log_action
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.api_tokens")
_scope_log = get_logger("persona.api_tokens.scopes")

# Re-exported so the template / tests can refer to a single source of
# truth. The actual whitelist lives in :mod:`app.api_tokens`.
_ALLOWED_SCOPES: tuple[str, ...] = ALLOWED_SCOPES


def _normalise_scopes(raw: str) -> str:
    """Filter a comma-separated scope string against :data:`ALLOWED_SCOPES`.

    Unknown items are dropped (logged at the model layer); if the
    whole input ends up empty we fall back to ``read`` so failing closed
    is the default. The result is itself comma-separated, ready to hand
    to :func:`create_token`.
    """
    candidates = [s.strip() for s in raw.split(",")]
    kept = [s for s in candidates if s in ALLOWED_SCOPES]
    seen: set[str] = set()
    deduped: list[str] = []
    for s in kept:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    if not deduped:
        return "read"
    return ",".join(deduped)


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
) -> RedirectResponse:
    """Mint a new token and bounce back with the raw value in the URL.

    The redirect is 303 so refreshing the resulting page is idempotent
    (no second token will be minted). The raw value is deliberately
    *not* stored in any server-side flash — it lives only on the user's
    screen until they navigate away.

    Scopes arrive either as repeated ``scopes`` checkbox fields (the
    template's preferred shape) or as a single comma-separated ``scopes``
    value (kept for curl users and the legacy v0.34 form). Both shapes
    are funnelled through :func:`_normalise_scopes` so the DB only ever
    sees whitelisted strings.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        # Bouncing back without a banner is the simplest possible
        # "validation failed" UX; the form keeps its values via the
        # browser's bfcache.
        await log_action(
            "api_token.create",
            target="",
            detail="empty name rejected",
            success=False,
        )
        return RedirectResponse(url="/settings/api-tokens", status_code=303)

    # Read the raw form so we can pick up the *list* of checkbox values
    # under the same ``scopes`` key — ``Form(default=...)`` would only
    # surface the last one.
    form = await request.form()
    scope_items_raw = form.getlist("scopes")
    scope_items = [str(item) for item in scope_items_raw]
    scopes_csv = ",".join(scope_items) if scope_items else "read"
    scope_string = _normalise_scopes(scopes_csv)
    raw = await create_token(name=cleaned_name, scopes=scope_string)
    # Never log the raw token value — only the name + scopes survive
    # in the audit trail. The raw string lives on the user's screen
    # for one render and is then gone.
    await log_action(
        "api_token.create",
        target=cleaned_name,
        detail="scopes=" + scope_string,
    )
    # The raw value is urlsafe by construction, so direct interpolation
    # is fine — no further encoding step required.
    return RedirectResponse(
        url=f"/settings/api-tokens?new_token={raw}",
        status_code=303,
    )


@router.post("/settings/api-tokens/{token_id}/revoke")
async def api_tokens_revoke(token_id: int) -> RedirectResponse:
    """Soft-revoke a token (sets ``revoked_at``) and return to the list."""
    changed = await revoke_token(token_id)
    await log_action(
        "api_token.revoke",
        target=str(token_id),
        detail="already revoked or unknown" if not changed else None,
        success=bool(changed),
    )
    return RedirectResponse(url="/settings/api-tokens", status_code=303)
