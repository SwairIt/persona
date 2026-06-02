"""Admin UI for per-app retention overrides.

Three endpoints surface here:

* ``GET /settings/app-retention`` — renders the table of existing
  overrides plus the add/edit form. Each row shows the four knobs and
  the ``never_delete`` switch so the operator can see at a glance which
  apps drift from the global policy.
* ``POST /settings/app-retention`` — form-encoded upsert. Empty numeric
  inputs map to ``None`` so the operator can override just one knob and
  let the rest inherit from :class:`app.settings.Settings`.
* ``POST /settings/app-retention/{app_name}/delete`` — removes the
  override row; the app reverts to the global policy on the next
  worker tick.

Validation lives in :mod:`app.app_retention`; this layer translates
HTTP <-> the helpers and renders the template.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.app_retention import (
    list_overrides,
    remove_override,
    set_override,
)
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.retention.per_app")

router = APIRouter(tags=["app-retention"])

# Cap on the "your most-captured apps without an override" sidecar — same
# shape :mod:`app.web.routes.app_overrides` uses for the capture-interval
# admin page so the two settings pages feel symmetrical.
_SUGGESTION_LIMIT = 12
_TOP_APPS_LIMIT = 64


def _parse_optional_int(raw: str | None) -> int | None:
    """Return ``None`` for blank input, otherwise parse as ``int``.

    The form uses ``type="number"`` inputs which FastAPI surfaces as
    optional strings; an empty value means "inherit from settings" and
    must not become ``0`` (which would silently delete every screenshot
    on the next worker tick).
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        msg = f"expected an integer, got {raw!r}"
        raise ValueError(msg) from exc


@router.get("/settings/app-retention", response_class=HTMLResponse)
async def app_retention_page(request: Request) -> HTMLResponse:
    """Render the per-app retention admin page."""
    settings = get_settings()
    items = await list_overrides()
    existing = {item["app_name"] for item in items}
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT ?",
            (_TOP_APPS_LIMIT,),
        )
        rows = await cursor.fetchall()
    suggested = [
        {"app_name": str(row["app_name"]), "count": int(row["n"])}
        for row in rows
        if str(row["app_name"]) not in existing
    ][:_SUGGESTION_LIMIT]

    return templates.TemplateResponse(
        request,
        "app_retention.html",
        {
            "title": "Per-app retention",
            "active_nav": "settings",
            "items": items,
            "suggested": suggested,
            "global_warm": settings.tier_warm_after_days,
            "global_cold": settings.tier_cold_after_days,
            "global_delete": settings.retention_days,
        },
    )


@router.post("/settings/app-retention")
async def app_retention_save(
    app_name: str = Form(...),
    warm_after_days: str | None = Form(None),
    cold_after_days: str | None = Form(None),
    delete_after_days: str | None = Form(None),
    never_delete: str | None = Form(None),
) -> RedirectResponse:
    """Upsert the override for ``app_name``.

    Empty numeric inputs map to ``None`` (inherit from settings).
    ``never_delete`` is a checkbox so its presence (any non-empty value)
    is treated as "on" — its absence as "off".
    """
    try:
        warm = _parse_optional_int(warm_after_days)
        cold = _parse_optional_int(cold_after_days)
        delete = _parse_optional_int(delete_after_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    never = never_delete is not None and never_delete.strip() != ""

    try:
        await set_override(
            app_name=app_name,
            warm=warm,
            cold=cold,
            delete=delete,
            never=never,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url="/settings/app-retention", status_code=303)


@router.post("/settings/app-retention/{app_name}/delete")
async def app_retention_delete(app_name: str) -> RedirectResponse:
    """Delete the override for ``app_name`` and redirect back."""
    try:
        await remove_override(app_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings/app-retention", status_code=303)


__all__ = ["router"]
