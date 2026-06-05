"""Mobile bottom-nav fragment — opt-in iOS-style 5-icon bar for <md screens.

The default ``base.html`` v1.17 shell exposes a hamburger drawer on the
narrow viewport. For screenshots and marketing the hamburger looks
clunky next to native apps — a fixed-bottom 5-icon bar reads instantly
as "this is mobile". This feature ships that bar as a *fragment* the
operator can opt into without touching the global shell.

How it works
------------
* A single kv row ``mobile_bottom_nav_enabled`` (default ``"0"``) gates
  the feature. While the row is ``"0"`` the fragment endpoint still
  responds (so the admin preview iframe always renders); the gating
  happens client-side, in the snippet ``base.html`` injects.
* :func:`mobile_bottom_nav_fragment` renders the standalone bar from
  :mod:`_mobile_bottom_nav.html`. The bar is *server-rendered* — the
  current page passes its ``active_nav`` slug via the ``?active=…``
  query string and the template lights the matching icon.
* :func:`mobile_bottom_nav_admin` is the operator dashboard: a toggle
  form + an iframe preview pointed at ``/widget/mobile-bottom-nav``
  with each active state exercised. Extends ``base.html``.
* :func:`mobile_bottom_nav_api` flips the kv row from a JSON ``fetch``.

Routes
------
GET  /widget/mobile-bottom-nav    — fragment (HTMX-injectable)
GET  /admin/mobile-bottom-nav     — operator page (toggle + preview)
POST /api/mobile-bottom-nav       — JSON ``{enabled: bool}`` flips kv

All SQL is parametrised; the kv read/write goes through the existing
:func:`app.storage.repository.get_kv` / :func:`set_kv` helpers.

The route module does *not* register itself in ``app/web/main.py`` —
the harness wires routers there, the same way it does for
``focus_profiles`` and the audit-log rotation pages.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["mobile-bottom-nav"])
log = get_logger("persona.web.mobile_bottom_nav")

#: kv row name that gates the feature. Sharing the constant between the
#: read/write code keeps an accidental typo from silently desyncing the
#: toggle and the reader.
_KV_ENABLED: Final[str] = "mobile_bottom_nav_enabled"

#: When the row is missing we behave as if it were "0" — the feature
#: must be opt-in. Anything that trims to "1" / "true" / "yes" / "on"
#: counts as enabled; everything else (including empty / garbage) is off.
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: The five slugs the bar advertises. The order matches the desktop nav
#: in ``base.html`` v1.17 (timeline / search / ask / memory / settings)
#: so muscle-memory carries over when the operator widens the window.
_NAV_SLUGS: Final[tuple[str, ...]] = (
    "timeline",
    "search",
    "ask",
    "memory",
    "settings",
)


class _ToggleRequest(BaseModel):
    """JSON body for the POST endpoint — just an ``enabled`` boolean."""

    enabled: bool = Field(..., description="Turn the bottom nav on/off.")


async def _read_enabled() -> bool:
    """Read the kv row and coerce to a boolean.

    Centralised so the admin page, the fragment endpoint and any future
    server-side gating all agree on what "enabled" means.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def _sanitise_active(raw: str | None) -> str:
    """Trim and whitelist the ``?active=…`` slug.

    A typo'd slug (or a missing query string) renders the bar with no
    highlight — preferable to silently falling back to e.g. "timeline"
    because that would mislead the operator into thinking the active
    detection works when it doesn't.
    """
    if raw is None:
        return ""
    slug = raw.strip().lower()
    return slug if slug in _NAV_SLUGS else ""


@router.get("/widget/mobile-bottom-nav", response_class=HTMLResponse)
async def mobile_bottom_nav_fragment(
    request: Request, active: str | None = None
) -> HTMLResponse:
    """Render the standalone bottom-nav fragment.

    The endpoint always responds — gating is the caller's job — so the
    admin preview iframe can show the bar regardless of the kv state.
    The ``active`` query param accepts one of :data:`_NAV_SLUGS`; an
    unknown slug renders the bar with no highlight.
    """
    return templates.TemplateResponse(
        request,
        "_mobile_bottom_nav.html",
        {
            "active_nav": _sanitise_active(active),
            "nav_slugs": _NAV_SLUGS,
        },
    )


@router.get("/admin/mobile-bottom-nav", response_class=HTMLResponse)
async def mobile_bottom_nav_admin(request: Request) -> HTMLResponse:
    """Operator dashboard — toggle + iframe preview of the bar."""
    enabled = await _read_enabled()
    log.info("mobile_bottom_nav.admin.render", enabled=enabled)
    return templates.TemplateResponse(
        request,
        "mobile_bottom_nav_admin.html",
        {
            "title": "Mobile bottom nav",
            "active_nav": "settings",
            "enabled": enabled,
            "nav_slugs": _NAV_SLUGS,
            "kv_key": _KV_ENABLED,
        },
    )


@router.post("/api/mobile-bottom-nav", response_class=JSONResponse)
async def mobile_bottom_nav_api(payload: _ToggleRequest) -> JSONResponse:
    """Flip the kv row from a JSON ``fetch``.

    Returns the canonical state after the flip so the caller can
    re-render without a second round-trip. Pydantic v2 already rejects
    non-boolean payloads with 422, so we only need to guard against the
    DB failing — let that surface as a 500 from the connection layer.
    """
    new_value = "1" if payload.enabled else "0"
    try:
        async with get_connection() as conn:
            await set_kv(conn, _KV_ENABLED, new_value)
    except Exception as exc:
        log.error(
            "mobile_bottom_nav.api.write_failed",
            enabled=payload.enabled,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="kv write failed") from exc
    log.info("mobile_bottom_nav.api.toggled", enabled=payload.enabled)
    return JSONResponse({"ok": True, "enabled": payload.enabled})


__all__ = ["router"]
