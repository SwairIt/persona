"""HTTP surface for the v1.45 configurable hotkey bindings.

Four routes:

* ``GET  /settings/hotkeys``        — Tailwind table editor.
* ``POST /settings/hotkeys/{action}`` — JSON body ``{"key_combo": str}``;
                                       persists a new binding and
                                       returns the canonical row.
* ``POST /api/hotkeys/reset``      — Wipe and re-seed every binding to
                                       its catalogue default.
* ``GET  /api/hotkeys.json``       — JSON map ``{action: {key_combo,
                                       enabled}}`` consumed by
                                       :file:`static/hotkey_loader.js`
                                       on every page load.

This module does NOT register itself with the FastAPI app in
:mod:`app.web.main`. Per task spec, ``main.py`` is off-limits — a
follow-up wires it up with::

    from app.web.routes import hotkey_settings
    app.include_router(hotkey_settings.router)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from app.hotkey_bindings import (
    ACTION_CATALOGUE,
    list_bindings,
    reset_to_defaults,
    update_binding,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["settings", "hotkeys"])

log = get_logger("persona.hotkey_bindings.routes")


@router.get("/settings/hotkeys", response_class=HTMLResponse)
async def hotkey_settings_page(request: Request) -> HTMLResponse:
    """Render the editor table.

    Pulls the full set of bindings via :func:`list_bindings` (always
    one row per catalogue entry, even if the DB is empty) and hands
    them to :file:`hotkey_settings.html` for rendering.
    """
    rows = await list_bindings()
    return templates.TemplateResponse(
        request,
        "hotkey_settings.html",
        {
            "title": "Горячие клавиши",
            "active_nav": "settings",
            "rows": rows,
        },
    )


@router.post("/settings/hotkeys/{action}", response_class=JSONResponse)
async def hotkey_settings_update(action: str, request: Request) -> JSONResponse:
    """Persist a new binding for ``action``.

    Body must be a JSON object with a ``key_combo`` string. An unknown
    action surfaces as ``404 Not Found`` rather than a 500 so the
    front-end can react with a tidy "no such action" toast. A
    malformed ``key_combo`` (empty, NUL byte) becomes ``400 Bad
    Request`` for the same reason.
    """
    if action not in ACTION_CATALOGUE:
        log.warning("hotkey_bindings.update.unknown_action", action=action)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown hotkey action: {action!r}",
        )
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body must be valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body must be a JSON object",
        )
    key_combo_raw = payload.get("key_combo")
    if not isinstance(key_combo_raw, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key_combo must be a string",
        )
    try:
        await update_binding(action, key_combo_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # Re-read so the response carries the canonical (trimmed) combo.
    rows = await list_bindings()
    row = next(r for r in rows if r["action"] == action)
    return JSONResponse(
        {
            "action": row["action"],
            "key_combo": row["key_combo"],
            "default_combo": row["default_combo"],
            "enabled": row["enabled"],
        }
    )


@router.post("/api/hotkeys/reset", response_class=JSONResponse)
async def hotkey_settings_reset(request: Request) -> JSONResponse:
    """Reset every binding to its catalogue default.

    Returns ``{"reset": N}`` where ``N`` is the number of rows
    rewritten (always equal to ``len(ACTION_CATALOGUE)``). The page
    JS calls this on the "Reset" button and then re-fetches the
    bindings map.
    """
    _ = request  # unused; signature kept consistent with other routes
    written = await reset_to_defaults()
    return JSONResponse({"reset": written})


@router.get("/api/hotkeys.json", response_class=JSONResponse)
async def hotkey_settings_api(request: Request) -> JSONResponse:
    """Return the active bindings for :file:`static/hotkey_loader.js`.

    The JS layer fetches this on ``DOMContentLoaded`` and uses the map
    to register one global ``keydown`` listener per enabled action.
    Disabled rows are still returned (with ``enabled: false``) so a
    future settings UI can render them greyed out without a second
    round-trip.
    """
    _ = request  # unused; signature kept consistent with other routes
    rows = await list_bindings()
    payload = {
        row["action"]: {
            "key_combo": row["key_combo"],
            "enabled": row["enabled"],
        }
        for row in rows
    }
    return JSONResponse(payload)


__all__ = ["router"]
