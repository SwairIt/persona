"""Remote agents admin UI — list / create / revoke (v1.12).

``GET  /admin/agents``                renders the management page
``POST /admin/agents``                mints a fresh agent + token; the
                                      raw value is shown in a one-time
                                      green banner via a query param
``POST /admin/agents/{id}/revoke``    soft-revokes an agent

The raw token value is round-tripped through a redirect query parameter
so a browser refresh of the resulting page doesn't accidentally
re-issue a new agent. The value lives in the browser's history for a
single session and the user is told (loudly, in the banner) to copy it
now and never see it again.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up in a follow-up patch with::

    from app.web.routes import agents_admin as agents_admin_routes
    app.include_router(agents_admin_routes.router)
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.remote_agents import create_agent, list_agents, revoke_agent
from app.web.templates_engine import templates

router = APIRouter(tags=["admin-agents"])
log = get_logger("persona.remote_agent")


async def _require_owner(session: SessionRecord) -> None:
    """Defense-in-depth поверх auth_gate: управлять агентами (доступ к скриншотам/
    аудио) может ТОЛЬКО владелец. 403 для любого другого аккаунта."""
    if not await is_owner(session["user_id"]):
        raise HTTPException(status_code=403, detail="owner only")

# Whitelist for the ``platform`` form field. The DB column itself is
# free-form TEXT — older rows or future platforms keep validating — but
# the create-agent form constrains the operator to this set so a typo
# can't fragment the dashboard into mostly-empty filter buckets.
_ALLOWED_PLATFORMS: Final[tuple[str, ...]] = ("macos", "ios", "linux", "windows", "other")


def _normalise_platform(raw: str | None) -> str | None:
    """Filter the platform form field against :data:`_ALLOWED_PLATFORMS`.

    Returns ``None`` (stored as NULL) when the operator left it blank
    or picked something off the whitelist; otherwise the cleaned value.
    """
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    if cleaned not in _ALLOWED_PLATFORMS:
        return None
    return cleaned


@router.get("/admin/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    new_token: str | None = None,
) -> HTMLResponse:
    """Render the list of provisioned agents plus the create form.

    ``new_token`` only ever arrives via the POST→redirect handshake; it
    is *not* a documented API parameter and must never be linked to
    externally. We treat it as a plain string with no validation beyond
    "is it present" so the template can decide whether to show the
    one-time banner.
    """
    await _require_owner(session)
    agents = await list_agents(include_revoked=True)
    return templates.TemplateResponse(
        request,
        "agents_admin.html",
        {
            "title": "Remote agents",
            "active_nav": "settings",
            "agents": agents,
            "new_token": new_token,
            "allowed_platforms": _ALLOWED_PLATFORMS,
        },
    )


@router.post("/admin/agents")
async def agents_create(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    name: Annotated[str, Form(...)],
    platform: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Mint a new agent and bounce back with the raw token in the URL.

    The redirect is 303 so refreshing the resulting page is idempotent
    (no second agent will be minted). The raw value is deliberately
    *not* stored in any server-side flash — it lives only on the
    user's screen until they navigate away.

    The audit log entry stamps the agent *name* and *platform* only,
    never the raw token. ``persona.remote_agent.created`` is also
    emitted via structlog inside :func:`create_agent` so the action is
    visible in both the audit_log table and the structured log
    pipeline.
    """
    await _require_owner(session)
    cleaned_name = name.strip()
    if not cleaned_name:
        await log_action(
            "remote_agent.create",
            target="",
            detail="empty name rejected",
            success=False,
        )
        return RedirectResponse(url="/admin/agents", status_code=303)

    cleaned_platform = _normalise_platform(platform)

    agent_id, raw_token = await create_agent(cleaned_name, platform=cleaned_platform)
    await log_action(
        "remote_agent.create",
        target=str(agent_id),
        detail=f"name={cleaned_name} platform={cleaned_platform or '-'}",
    )
    # The raw value is urlsafe by construction, so direct interpolation
    # is fine — no further encoding step required. The banner template
    # then renders it once and instructs the operator to copy it.
    return RedirectResponse(
        url=f"/admin/agents?new_token={raw_token}",
        status_code=303,
    )


@router.post("/admin/agents/{agent_id}/revoke")
async def agents_revoke(
    agent_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Soft-revoke an agent (sets ``revoked_at``) and return to the list."""
    await _require_owner(session)
    changed = await revoke_agent(agent_id)
    await log_action(
        "remote_agent.revoke",
        target=str(agent_id),
        detail="already revoked or unknown" if not changed else None,
        success=bool(changed),
    )
    return RedirectResponse(url="/admin/agents", status_code=303)


__all__ = ["router"]
