"""Customisable keyboard shortcuts (v1.2 feature 1/3).

Lets the operator rebind the small set of single-key and multi-key
shortcuts wired up across :file:`keyboard_shortcuts.js`,
:file:`search_keyboard.js`, :file:`quick_pin.js`, and
:file:`image_viewer.js`. The shortcut map is persisted as a JSON blob in
``kv_settings`` under the key ``kbd_shortcuts_json`` — see migration
``083_kbd_shortcuts.sql`` for the default seed.

Routes wired here:

* ``GET  /settings/keyboard`` — Tailwind editor form.
* ``POST /settings/keyboard`` — persist a new map, then redirect.
* ``GET  /api/kbd-shortcuts.json`` — JSON map the front-end fetches
  once on load to override its built-in defaults.

Validation policy mirrors :mod:`app.i18n`: a corrupt or partial kv
edit collapses to the built-in defaults rather than 500ing the page or
wedging the listeners. Only known action names from
:data:`KBD_SHORTCUT_ACTIONS` are written back to the kv — extra keys
from a hand-crafted POST are silently dropped.
"""

from __future__ import annotations

import json
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings", "keyboard"])

_log = get_logger("persona.kbd_shortcuts")

# kv key shared with migration ``083_kbd_shortcuts.sql`` and the static
# JS modules that fetch ``/api/kbd-shortcuts.json``. Single source of
# truth so a rename can't drift writer and reader.
KBD_SHORTCUTS_KV_KEY: Final[str] = "kbd_shortcuts_json"

# Action whitelist + defaults. The string values are exactly the keys
# the existing hard-coded listeners check, so a fresh install with no
# kv row (and even one with a malformed row) behaves identically to
# every previous version of Persona.
#
# Multi-key "g+letter" sequences are encoded as two whitespace-separated
# tokens (``"g t"``). The front-end splits on whitespace, treats a
# single token as an instant bind, and a two-token entry as a sequence
# with the canonical 1500ms timeout.
KBD_SHORTCUT_ACTIONS: Final[dict[str, str]] = {
    "help_overlay": "?",
    "search_focus": "/",
    "go_timeline": "g t",
    "pin_toggle": "p",
    "fullscreen": "f",
}

# Human-readable labels for the editor form. Kept here rather than in
# the template so the same labels can later be reused by the JSON API
# / a future settings export. Order is the order the form rows render.
KBD_SHORTCUT_LABELS: Final[dict[str, str]] = {
    "help_overlay": "Show keyboard shortcuts cheatsheet",
    "search_focus": "Focus search input",
    "go_timeline": "Go to Timeline (multi-key)",
    "pin_toggle": "Toggle pin on active shot",
    "fullscreen": "Fullscreen the active image",
}


def _coerce_map(raw: str | None) -> dict[str, str]:
    """Decode a kv JSON blob into a complete shortcut map.

    Any unknown action key is dropped, any missing action key is filled
    from :data:`KBD_SHORTCUT_ACTIONS`, and a malformed blob collapses to
    the defaults outright. The returned dict always has *exactly* the
    same keys as :data:`KBD_SHORTCUT_ACTIONS` so the renderer and the
    JSON endpoint never have to special-case "missing action".
    """
    merged = dict(KBD_SHORTCUT_ACTIONS)
    if not raw:
        return merged
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log.warning("kbd_shortcuts.parse_failed", error=str(exc))
        return merged
    if not isinstance(parsed, dict):
        _log.warning("kbd_shortcuts.parse_not_dict", type=type(parsed).__name__)
        return merged
    for action, default in KBD_SHORTCUT_ACTIONS.items():
        candidate = parsed.get(action)
        if isinstance(candidate, str):
            normalised = candidate.strip()
            if normalised:
                merged[action] = normalised
                continue
        merged[action] = default
    return merged


def _normalise_binding(value: str) -> str:
    """Trim and collapse internal whitespace in a user-supplied binding.

    A user typing ``"g  t"`` or ``" g t "`` into the form should produce
    the same canonical ``"g t"`` token the front-end splits on. We do
    NOT lowercase here — case sensitivity is the front-end's call (it
    already handles ``g``/``G`` symmetrically in keyboard_shortcuts.js).
    """
    return " ".join(value.split())


@router.get("/settings/keyboard", response_class=HTMLResponse)
async def kbd_shortcuts_page(request: Request) -> HTMLResponse:
    """Render the keyboard-shortcut editor (Tailwind form)."""
    async with get_connection() as conn:
        raw = await get_kv(conn, KBD_SHORTCUTS_KV_KEY)
    current = _coerce_map(raw)
    rows = [
        {
            "action": action,
            "label": KBD_SHORTCUT_LABELS[action],
            "default": KBD_SHORTCUT_ACTIONS[action],
            "current": current[action],
        }
        for action in KBD_SHORTCUT_ACTIONS
    ]
    return templates.TemplateResponse(
        request,
        "kbd_shortcuts.html",
        {
            "title": "Keyboard shortcuts",
            "active_nav": "settings",
            "rows": rows,
        },
    )


@router.post("/settings/keyboard", response_class=HTMLResponse)
async def kbd_shortcuts_save(request: Request) -> RedirectResponse:
    """Persist a new shortcut map.

    The form posts one field per action (``help_overlay``,
    ``search_focus``, …). Empty fields collapse to the built-in default
    so a user can blank out a binding to "reset this one row". Unknown
    fields are silently ignored.
    """
    form = await request.form()
    new_map: dict[str, str] = {}
    for action, default in KBD_SHORTCUT_ACTIONS.items():
        raw_value = form.get(action)
        value = _normalise_binding(str(raw_value)) if raw_value is not None else ""
        new_map[action] = value or default
    encoded = json.dumps(new_map, separators=(",", ":"), sort_keys=True)
    async with get_connection() as conn:
        await set_kv(conn, KBD_SHORTCUTS_KV_KEY, encoded)
    _log.info(
        "kbd_shortcuts.save",
        actions=len(new_map),
        source="settings_ui",
    )
    return RedirectResponse(url="/settings/keyboard", status_code=303)


@router.get("/api/kbd-shortcuts.json", response_class=JSONResponse)
async def kbd_shortcuts_api(request: Request) -> JSONResponse:
    """Return the current shortcut map for the front-end to consume.

    Front-end JS modules (``keyboard_shortcuts.js``, ``quick_pin.js``,
    ``image_viewer.js``, ``search_keyboard.js``) call this once on load
    and override their built-in defaults with the user's bindings. A
    fetch failure on the front-end falls back to the same defaults this
    endpoint would have returned, so an offline / errored fetch is a
    no-op rather than a broken page.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, KBD_SHORTCUTS_KV_KEY)
    payload = _coerce_map(raw)
    return JSONResponse(payload)


__all__ = [
    "KBD_SHORTCUTS_KV_KEY",
    "KBD_SHORTCUT_ACTIONS",
    "KBD_SHORTCUT_LABELS",
    "router",
]
