"""UI theme settings — GET form, POST persists to ``kv_settings`` (v0.32).

A single ``theme`` row in ``kv_settings`` drives the ``<html>`` class on
:file:`base.html`. The whitelist is intentionally minimal — ``dark``,
``light``, ``auto`` — so the POST handler can reject anything else with
a 400 instead of writing junk that would silently fall back to the
default on the next render.

``auto`` defers to the user's OS preference via ``window.matchMedia``;
the inline script lives in :file:`base.html` because it must run before
first paint to avoid a flash of the wrong palette.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import invalidate_theme_cache, templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.theme")

_VALID_THEMES: Final[frozenset[str]] = frozenset({"dark", "light", "auto", "persona"})
_DEFAULT_THEME: Final[str] = "persona"


async def _load_theme() -> str:
    """Read the ``theme`` row, defaulting to ``dark`` if absent or bogus."""
    async with get_connection() as conn:
        raw = await get_kv(conn, "theme")
    if raw is None or raw not in _VALID_THEMES:
        return _DEFAULT_THEME
    return raw


@router.get("/settings/theme", response_class=HTMLResponse)
async def theme_settings_page(request: Request) -> HTMLResponse:
    """Render the three-radio form with the stored theme pre-selected."""
    current = await _load_theme()
    return templates.TemplateResponse(
        request,
        "theme_settings.html",
        {
            "title": "Theme",
            "active_nav": "settings",
            "current": current,
            "options": (
                ("persona", "Persona ✨", "Фиолетовая тема в стиле лендинга — градиенты и стекло."),
                ("dark", "Dark", "Тёмная минималистичная палитра."),
                ("light", "Light", "Светлая палитра для яркой комнаты."),
                ("auto", "Auto", "Следовать системной теме ОС."),
            ),
        },
    )


@router.post("/settings/theme")
async def theme_settings_save(
    request: Request,
    theme: str = Form(...),
) -> RedirectResponse:
    """Validate against the whitelist and persist; reject anything else."""
    value = theme.strip().lower()
    if value not in _VALID_THEMES:
        log.warning("theme.settings.rejected", value=value)
        raise HTTPException(status_code=400, detail="invalid theme")

    async with get_connection() as conn:
        await set_kv(conn, "theme", value)

    # The Jinja global caches per-request; this POST already cached the
    # *previous* value during form-render-time helpers, so drop it so
    # the redirect-target render reflects the new value immediately.
    invalidate_theme_cache()

    log.info("theme.settings.saved", theme=value)
    return RedirectResponse(url="/settings/theme", status_code=303)
