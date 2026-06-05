"""Workspace management UI + JSON snapshot (v1.64).

A *workspace* bundles theme + capture cadence + focus profile +
blocklist + a default timeline filter into one row of the ``workspace``
table. Activating a workspace flips every relevant kv row AND chains
through to the v1.49 focus-profile helper in one click — see
:mod:`app.workspaces` for the helper layer.

Routes
------
GET  /workspaces                          — grid of workspaces + preset cards
POST /workspaces/{id}/activate            — switch to this workspace
POST /workspaces/new                      — create a custom workspace (form)
POST /workspaces/install-preset/{name}    — install a built-in preset
POST /workspaces/{id}/delete              — remove a workspace (idempotent)
GET  /api/workspaces.json                 — JSON snapshot for tooling

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form. The route file is intentionally self-contained: it
does not register itself in ``app/web/main.py``; the parent harness
wires routers elsewhere.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.focus_profiles import list_profiles
from app.logging_setup import get_logger
from app.web.templates_engine import (
    invalidate_theme_cache,
    templates,
)
from app.workspaces import (
    PRESET_WORKSPACES,
    activate_workspace,
    create_workspace,
    delete_workspace,
    install_preset,
    list_workspaces,
)

router = APIRouter(tags=["workspaces"])
log = get_logger("persona.workspaces")

_VALID_THEMES: frozenset[str] = frozenset({"dark", "light", "auto"})


@router.get("/workspaces", response_class=HTMLResponse)
async def workspaces_page(request: Request) -> HTMLResponse:
    """Render the workspace management page.

    The view splits the screen into four sections: the currently active
    workspace chip, the grid of saved workspaces (each with an
    *Activate* / *Delete* button), the install-preset cards for the
    three built-in presets the operator has not yet installed, and a
    create-new form.
    """
    workspaces = await list_workspaces()
    installed_names = {workspace["name"] for workspace in workspaces}
    pending_presets = [
        preset for preset in PRESET_WORKSPACES if preset["name"] not in installed_names
    ]
    active = next(
        (workspace for workspace in workspaces if workspace["is_active"]),
        None,
    )
    focus_profiles = await list_profiles()
    return templates.TemplateResponse(
        request,
        "workspaces.html",
        {
            "title": "Рабочие пространства",
            "active_nav": "settings",
            "workspaces": workspaces,
            "pending_presets": pending_presets,
            "active_workspace": active,
            "focus_profiles": focus_profiles,
            "valid_themes": sorted(_VALID_THEMES),
        },
    )


@router.post("/workspaces/{ws_id}/activate")
async def workspaces_activate(ws_id: int) -> RedirectResponse:
    """Activate ``ws_id`` and apply its kv + focus-profile bundle.

    Surfaces a 404 when the workspace id is unknown so a stale form
    submission does not 500 the page. After the activation we drop the
    per-request theme cache so the next render reflects the new
    ``theme`` kv value if the workspace flipped it.
    """
    try:
        await activate_workspace(ws_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    invalidate_theme_cache()
    return RedirectResponse(url="/workspaces", status_code=303)


@router.post("/workspaces/install-preset/{name}")
async def workspaces_install_preset(name: str) -> RedirectResponse:
    """Install one of the built-in presets by name.

    Unknown preset names surface as a 400 so a typo'd POST does not
    silently no-op. Re-installing an existing preset is idempotent —
    :func:`install_preset` uses ``INSERT OR IGNORE``.
    """
    try:
        await install_preset(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/workspaces", status_code=303)


@router.post("/workspaces/new")
async def workspaces_create(
    name: Annotated[str, Form(...)],
    description: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
    capture_interval_seconds: Annotated[str, Form()] = "",
    focus_profile_id: Annotated[str, Form()] = "",
    blocklist_apps: Annotated[str, Form()] = "",
    default_timeline_filter: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a custom workspace from the form on /workspaces.

    Form fields use the empty-string convention for "unset" so the
    operator can submit a workspace that only flips one knob:

    * empty ``theme`` → NULL (do not touch the kv row)
    * empty ``capture_interval_seconds`` → NULL (same)
    * empty ``focus_profile_id`` → NULL (do not chain)
    * empty ``blocklist_apps`` → ``[]``
    * empty ``default_timeline_filter`` → NULL

    The interval is parsed with ``float`` so the operator can type
    ``"7.5"`` for a sub-second cadence in tests. Out-of-range and
    unparseable values surface as 400. ``blocklist_apps`` is a
    comma-separated string in the form; we split + strip into the JSON
    list the helper layer wants.
    """
    try:
        interval_value: float | None
        if capture_interval_seconds.strip():
            interval_value = float(capture_interval_seconds)
            if interval_value <= 0:
                msg = "capture_interval_seconds must be positive"
                raise ValueError(msg)
        else:
            interval_value = None
        cleaned_theme = theme.strip() or None
        if cleaned_theme is not None and cleaned_theme not in _VALID_THEMES:
            msg = f"theme must be one of {sorted(_VALID_THEMES)}"
            raise ValueError(msg)
        focus_value: int | None
        if focus_profile_id.strip():
            focus_value = int(focus_profile_id)
        else:
            focus_value = None
        blocklist_list = [
            item.strip() for item in blocklist_apps.split(",") if item.strip()
        ]
        await create_workspace(
            name=name,
            description=description or None,
            theme=cleaned_theme,
            capture_interval_seconds=interval_value,
            focus_profile_id=focus_value,
            blocklist_apps=blocklist_list,
            default_timeline_filter=default_timeline_filter or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/workspaces", status_code=303)


@router.post("/workspaces/{ws_id}/delete")
async def workspaces_delete(ws_id: int) -> RedirectResponse:
    """Delete the given workspace. Idempotent."""
    await delete_workspace(ws_id)
    return RedirectResponse(url="/workspaces", status_code=303)


@router.get("/api/workspaces.json", response_class=JSONResponse)
async def workspaces_json() -> JSONResponse:
    """JSON snapshot of every workspace for tooling / the client clock."""
    workspaces = await list_workspaces()
    return JSONResponse(
        {
            "workspaces": [dict(workspace) for workspace in workspaces],
            "active": next(
                (dict(workspace) for workspace in workspaces if workspace["is_active"]),
                None,
            ),
        }
    )
