"""Admin UI for the privacy-mode bundle library (v1.43).

Writeable counterpart to :mod:`app.web.routes.privacy_mode_admin`.
That page is intentionally read-only — the v1.42 hard-coded
``PRIVACY_PATTERNS`` tuple cannot be edited at runtime. v1.43 layers
*bundles* on top: named, grouped pattern lists the operator can
install from preset cards or grow by hand, while the hard-coded
tuple stays as the safety floor.

Routes:

    GET  /privacy-mode/bundles                       — list + forms
    POST /privacy-mode/bundles/new                   — create empty bundle
    POST /privacy-mode/bundles/{id}/pattern          — append a pattern
    POST /privacy-mode/bundles/{id}/toggle           — flip enabled
    POST /privacy-mode/bundles/{id}/delete           — drop (CASCADE)
    POST /privacy-mode/bundles/install/{preset_name} — install preset
    GET  /api/privacy-bundles.json                   — JSON dump

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form. Every write path invalidates the
:mod:`app.privacy_mode` compile cache so the next capture iteration
observes the fresh pattern set.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.privacy_bundles import (
    PRESET_BUNDLES,
    add_pattern_to_bundle,
    create_bundle,
    delete_bundle,
    install_preset,
    list_bundles,
    list_patterns_for_bundle,
    toggle_bundle,
)
from app.privacy_mode import invalidate_active_patterns_cache
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.privacy_bundles.admin")

router = APIRouter(tags=["privacy-bundles"])


@router.get("/privacy-mode/bundles", response_class=HTMLResponse)
async def privacy_bundles_page(request: Request) -> HTMLResponse:
    """Render the bundle-management page.

    Includes: list of installed bundles with toggle/delete + Add Pattern
    sub-form per bundle; a top-level "create empty bundle" form; the
    set of preset cards still available to install (already-installed
    presets are hidden so the operator does not double-tap).
    """
    async with get_connection() as conn:
        bundles = await list_bundles(conn)
        patterns_by_bundle: dict[int, list[dict[str, Any]]] = {}
        for bundle in bundles:
            bundle_id = int(bundle["id"])
            patt_rows = await list_patterns_for_bundle(conn, bundle_id)
            patterns_by_bundle[bundle_id] = [
                {"id": int(row["id"]), "pattern": str(row["pattern"])}
                for row in patt_rows
            ]

    installed_names = {str(row["name"]) for row in bundles}
    available_presets = [
        preset for preset in PRESET_BUNDLES if preset["name"] not in installed_names
    ]
    return templates.TemplateResponse(
        request,
        "privacy_bundles.html",
        {
            "title": "Privacy bundles",
            "active_nav": "settings",
            "bundles": bundles,
            "patterns_by_bundle": patterns_by_bundle,
            "available_presets": available_presets,
            "all_presets": PRESET_BUNDLES,
        },
    )


@router.post("/privacy-mode/bundles/new")
async def privacy_bundles_create(
    name: str = Form(...),
    description: str = Form(""),
) -> RedirectResponse:
    """Create an empty bundle, then 303-redirect back to the list."""
    try:
        async with get_connection() as conn:
            await create_bundle(conn, name=name, description=description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_active_patterns_cache()
    return RedirectResponse(url="/privacy-mode/bundles", status_code=303)


@router.post("/privacy-mode/bundles/{bundle_id}/pattern")
async def privacy_bundles_add_pattern(
    bundle_id: int,
    pattern: str = Form(...),
) -> RedirectResponse:
    """Append a pattern to ``bundle_id``, then 303-redirect."""
    try:
        async with get_connection() as conn:
            await add_pattern_to_bundle(
                conn,
                bundle_id=bundle_id,
                pattern=pattern,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    invalidate_active_patterns_cache()
    return RedirectResponse(url="/privacy-mode/bundles", status_code=303)


@router.post("/privacy-mode/bundles/{bundle_id}/toggle")
async def privacy_bundles_toggle(bundle_id: int) -> RedirectResponse:
    """Flip ``enabled`` on ``bundle_id``, then 303-redirect."""
    async with get_connection() as conn:
        await toggle_bundle(conn, bundle_id)
    invalidate_active_patterns_cache()
    return RedirectResponse(url="/privacy-mode/bundles", status_code=303)


@router.post("/privacy-mode/bundles/{bundle_id}/delete")
async def privacy_bundles_delete(bundle_id: int) -> RedirectResponse:
    """Delete ``bundle_id`` (CASCADE drops its patterns), then 303."""
    async with get_connection() as conn:
        await delete_bundle(conn, bundle_id)
    invalidate_active_patterns_cache()
    return RedirectResponse(url="/privacy-mode/bundles", status_code=303)


@router.post("/privacy-mode/bundles/install/{preset_name}")
async def privacy_bundles_install_preset(preset_name: str) -> RedirectResponse:
    """Install a preset bundle by name. Idempotent (see install_preset)."""
    result = await install_preset(preset_name)
    if result["bundle_id"] == 0 and not result["skipped_duplicate"]:
        # No such preset — surface as 404 so the admin UI does not
        # silently swallow a typo in a hard-coded ``action=`` URL.
        raise HTTPException(
            status_code=404,
            detail=f"unknown preset: {preset_name}",
        )
    log.info(
        "privacy_bundles.preset_install_response",
        preset=preset_name,
        bundle_id=result["bundle_id"],
        inserted=result["inserted"],
        skipped_duplicate=result["skipped_duplicate"],
    )
    return RedirectResponse(url="/privacy-mode/bundles", status_code=303)


@router.get("/api/privacy-bundles.json")
async def privacy_bundles_json() -> JSONResponse:
    """Return every bundle + patterns as JSON. Includes disabled rows.

    Shape: ``[{id, name, description, enabled, created_at,
    pattern_count, patterns: [{id, pattern}, ...]}, ...]``. Used by
    external tooling that wants to audit which sensitive surfaces
    privacy mode is configured to shield.
    """
    async with get_connection() as conn:
        bundles = await list_bundles(conn)
        payload: list[dict[str, Any]] = []
        for bundle in bundles:
            bundle_id = int(bundle["id"])
            patt_rows = await list_patterns_for_bundle(conn, bundle_id)
            payload.append(
                {
                    "id": bundle_id,
                    "name": str(bundle["name"]),
                    "description": (
                        str(bundle["description"])
                        if bundle["description"] is not None
                        else None
                    ),
                    "enabled": bool(bundle["enabled"]),
                    "created_at": str(bundle["created_at"]),
                    "pattern_count": int(bundle["pattern_count"]),
                    "patterns": [
                        {
                            "id": int(row["id"]),
                            "pattern": str(row["pattern"]),
                        }
                        for row in patt_rows
                    ],
                }
            )
    return JSONResponse(payload)


__all__ = ["router"]
