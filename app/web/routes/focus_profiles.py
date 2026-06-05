"""Focus-profile management UI + JSON snapshot (v1.49).

A *focus profile* bundles the capture cadence, screen kill switch, mic
kill switch, meeting-detector toggle and theme into one row of the
``focus_profile`` table. Activating a profile is a single HTTP POST
that flips every relevant kv row in one transaction — see
:mod:`app.focus_profiles` for the helper layer.

Routes
------
GET  /focus/profiles                          — grid of profiles + preset install cards
POST /focus/profiles/{id}/activate            — switch to this profile
POST /focus/profiles/install-preset/{name}    — install a built-in preset
POST /focus/profiles/new                      — create a custom profile (form)
POST /focus/profiles/{id}/delete              — remove a profile (idempotent)
GET  /api/focus/profiles.json                 — JSON snapshot for tooling

All POSTs end in a 303 redirect (PRG) so a browser refresh does not
re-submit the form. The route file is intentionally self-contained: it
does not register itself in ``app/web/main.py``; the parent harness wires
routers elsewhere.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.focus_profiles import (
    PRESET_PROFILES,
    activate_profile,
    create_profile,
    delete_profile,
    install_preset,
    list_profiles,
)
from app.logging_setup import get_logger
from app.web.templates_engine import (
    invalidate_theme_cache,
    templates,
)

router = APIRouter(tags=["focus-profiles"])
log = get_logger("persona.focus_profiles")

_VALID_THEMES: frozenset[str] = frozenset({"dark", "light", "auto"})


@router.get("/focus/profiles", response_class=HTMLResponse)
async def focus_profiles_page(request: Request) -> HTMLResponse:
    """Render the focus-profile management page.

    The view splits the screen into three sections: the currently active
    profile chip, the grid of saved profiles (each with an *Activate* /
    *Delete* button), and the install-preset cards for the four built-in
    presets the operator has not yet installed.
    """
    profiles = await list_profiles()
    installed_names = {profile["name"] for profile in profiles}
    pending_presets = [
        preset for preset in PRESET_PROFILES if preset["name"] not in installed_names
    ]
    active = next((profile for profile in profiles if profile["is_active"]), None)
    return templates.TemplateResponse(
        request,
        "focus_profiles.html",
        {
            "title": "Профили фокуса",
            "active_nav": "focus",
            "profiles": profiles,
            "pending_presets": pending_presets,
            "active_profile": active,
            "valid_themes": sorted(_VALID_THEMES),
        },
    )


@router.post("/focus/profiles/{profile_id}/activate")
async def focus_profiles_activate(profile_id: int) -> RedirectResponse:
    """Activate ``profile_id`` and apply its kv rows.

    Surfaces a 404 when the profile id is unknown so a stale form
    submission does not 500 the page. After the activation we drop the
    per-request theme cache so the next render reflects the new ``theme``
    kv value if the profile flipped it.
    """
    try:
        await activate_profile(profile_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    invalidate_theme_cache()
    return RedirectResponse(url="/focus/profiles", status_code=303)


@router.post("/focus/profiles/install-preset/{name}")
async def focus_profiles_install_preset(name: str) -> RedirectResponse:
    """Install one of the built-in presets by name.

    Unknown preset names surface as a 400 so a typo'd POST does not
    silently no-op. Re-installing an existing preset is idempotent —
    :func:`install_preset` uses ``INSERT OR IGNORE``.
    """
    try:
        await install_preset(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/focus/profiles", status_code=303)


@router.post("/focus/profiles/new")
async def focus_profiles_create(
    name: Annotated[str, Form(...)],
    description: Annotated[str, Form()] = "",
    capture_interval_seconds: Annotated[str, Form()] = "",
    audio_paused: Annotated[str, Form()] = "",
    blocklist_apps: Annotated[str, Form()] = "",
    meeting_pause_enabled: Annotated[str, Form()] = "",
    theme: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a custom focus profile from the form on /focus/profiles.

    Form fields use the empty-string convention for "unset" so the
    operator can submit a profile that only flips one knob:

    * empty ``capture_interval_seconds`` → NULL (do not touch the kv row)
    * empty ``theme`` → NULL (same)
    * unchecked ``audio_paused`` / ``meeting_pause_enabled`` → ``False``

    The interval is parsed with ``float`` so the operator can type
    ``"7.5"`` for a sub-second cadence in tests. Out-of-range and
    unparseable values surface as 400.
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
        await create_profile(
            name=name,
            description=description or None,
            capture_interval_seconds=interval_value,
            audio_paused=bool(audio_paused),
            blocklist_apps=blocklist_apps or None,
            meeting_pause_enabled=bool(meeting_pause_enabled),
            theme=cleaned_theme,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/focus/profiles", status_code=303)


@router.post("/focus/profiles/{profile_id}/delete")
async def focus_profiles_delete(profile_id: int) -> RedirectResponse:
    """Delete the given focus profile. Idempotent."""
    await delete_profile(profile_id)
    return RedirectResponse(url="/focus/profiles", status_code=303)


@router.get("/api/focus/profiles.json", response_class=JSONResponse)
async def focus_profiles_json() -> JSONResponse:
    """JSON snapshot of every focus profile for tooling / the client clock."""
    profiles = await list_profiles()
    return JSONResponse(
        {
            "profiles": [dict(profile) for profile in profiles],
            "active": next(
                (dict(profile) for profile in profiles if profile["is_active"]),
                None,
            ),
        }
    )
