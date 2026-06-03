"""Settings page — display config + kv overrides + Tesseract probe."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    UI_LANGUAGE_KV_KEY,
    invalidate_language_cache,
)
from app.logging_setup import get_logger
from app.ocr import probe_tesseract
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, list_kv, set_kv
from app.web.templates_engine import (
    invalidate_compact_cache,
    invalidate_grayscale_cache,
    invalidate_reduce_motion_cache,
    templates,
)

router = APIRouter(tags=["settings"])

_fomo_log = get_logger("persona.digest.fomo")
_compact_log = get_logger("persona.compact")
_grayscale_log = get_logger("persona.grayscale")
_reduce_motion_log = get_logger("persona.reduce_motion")
_i18n_log = get_logger("persona.i18n")

# kv key shared with ``app.llm.summariser`` and ``app.llm.weekly_summariser``.
# Kept as a route-level constant so the checkbox form-field name and the
# digest read path stay in lockstep — change in one place only.
_ANTI_FOMO_KV_KEY = "anti_fomo_digest"

# kv key shared with :mod:`app.web.templates_engine` (the ``get_compact_mode``
# Jinja global) — single source of truth so a rename can't drift the
# writer and reader out of sync.
_COMPACT_MODE_KV_KEY = "compact_mode"

# kv key shared with :mod:`app.web.templates_engine` (the
# ``get_grayscale_mode`` Jinja global) — single source of truth so a
# rename can't drift the writer and reader out of sync. v0.78.
_GRAYSCALE_MODE_KV_KEY = "grayscale_mode"

# kv key shared with :mod:`app.web.templates_engine` (the
# ``get_reduce_motion`` Jinja global) — single source of truth so a
# rename can't drift the writer and reader out of sync. v0.93.
_REDUCE_MOTION_KV_KEY = "reduce_motion"


def _parse_anti_fomo_kv(raw: str | None) -> bool | None:
    """Return ``True``/``False`` for a kv string, ``None`` if absent."""
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised == "":
        return None
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    return None


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the settings page."""
    cfg = get_settings()
    probe = probe_tesseract(cfg.tesseract_path)

    async with get_connection() as conn:
        overrides = await list_kv(conn)
        anti_fomo_kv = _parse_anti_fomo_kv(await get_kv(conn, _ANTI_FOMO_KV_KEY))
        compact_raw = await get_kv(conn, _COMPACT_MODE_KV_KEY)
        grayscale_raw = await get_kv(conn, _GRAYSCALE_MODE_KV_KEY)
        reduce_motion_raw = await get_kv(conn, _REDUCE_MOTION_KV_KEY)
        ui_language_raw = await get_kv(conn, UI_LANGUAGE_KV_KEY)

    # Effective state for the checkbox: kv override wins, env flag is the
    # fallback. Surfacing the env baseline separately lets the template
    # show a hint when the two diverge.
    anti_fomo_effective = (
        anti_fomo_kv if anti_fomo_kv is not None else bool(cfg.anti_fomo_digest)
    )
    # Compact mode is kv-only (no env baseline) — anything other than
    # the literal ``"1"`` collapses to "off" so the checkbox state mirrors
    # what the body attribute will actually carry on the next render.
    compact_enabled = (compact_raw or "").strip() == "1"
    # Grayscale mode follows the same kv-only ``"1"`` / ``"0"`` shape as
    # compact mode — keeps the checkbox and the body attribute in sync.
    grayscale_enabled = (grayscale_raw or "").strip() == "1"
    # Reduce-motion follows the same kv-only ``"1"`` / ``"0"`` shape —
    # keeps the checkbox state aligned with the body attribute that
    # gates ``reduce_motion.css``.
    reduce_motion_enabled = (reduce_motion_raw or "").strip() == "1"
    # UI language: anything outside the whitelist (or absent) collapses
    # to the default so the ``<select>`` always has a valid option
    # pre-selected and a manual kv edit can never wedge the renderer.
    ui_language_current = (ui_language_raw or "").strip()
    if ui_language_current not in SUPPORTED_LANGUAGES:
        ui_language_current = DEFAULT_LANGUAGE

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "title": "Settings",
            "active_nav": "settings",
            "settings": cfg,
            "overrides": overrides,
            "tesseract": probe,
            "anti_fomo_enabled": anti_fomo_effective,
            "anti_fomo_env_default": bool(cfg.anti_fomo_digest),
            "anti_fomo_kv_set": anti_fomo_kv is not None,
            "compact_mode_enabled": compact_enabled,
            "grayscale_mode_enabled": grayscale_enabled,
            "reduce_motion_enabled": reduce_motion_enabled,
            "ui_language_current": ui_language_current,
            "ui_language_options": sorted(SUPPORTED_LANGUAGES),
        },
    )


@router.post("/settings/override", response_class=HTMLResponse)
async def update_override(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
) -> RedirectResponse:
    """Upsert a kv override. Currently informational — no live reload of Settings."""
    async with get_connection() as conn:
        await set_kv(conn, key, value)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/anti-fomo-digest", response_class=HTMLResponse)
async def update_anti_fomo_digest(
    request: Request,
    enabled: str = Form(default=""),
) -> RedirectResponse:
    """Persist the anti-FOMO digest checkbox to ``kv_settings``.

    HTML checkboxes only POST a value when ticked, so the absence of the
    ``enabled`` field is treated as "off". We always write a kv row
    (rather than deleting on "off") so the user's explicit choice wins
    over the env flag both ways.
    """
    new_value = enabled.strip().lower() in {"1", "true", "yes", "on"}
    async with get_connection() as conn:
        await set_kv(conn, _ANTI_FOMO_KV_KEY, "true" if new_value else "false")
    _fomo_log.info(
        "digest.fomo.toggle",
        enabled=new_value,
        source="settings_ui",
    )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/compact-mode", response_class=HTMLResponse)
async def update_compact_mode(
    request: Request,
    enabled: str = Form(default=""),
) -> RedirectResponse:
    """Persist the compact-mode checkbox to ``kv_settings`` (v0.61).

    HTML checkboxes only POST a value when ticked, so an empty
    ``enabled`` field is treated as "off". The kv row is normalised to
    the literal ``"1"`` / ``"0"`` strings the Jinja global +
    ``compact_mode.css`` selector consume — anything else would silently
    fall back to "off" on the next render.

    Invalidates the per-request cache so the redirect-target render
    reflects the new value rather than the value cached earlier in this
    same request when the GET form was rendered.
    """
    new_value = enabled.strip().lower() in {"1", "true", "yes", "on"}
    async with get_connection() as conn:
        await set_kv(conn, _COMPACT_MODE_KV_KEY, "1" if new_value else "0")
    invalidate_compact_cache()
    _compact_log.info(
        "compact.toggle",
        enabled=new_value,
        source="settings_ui",
    )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/grayscale-mode", response_class=HTMLResponse)
async def update_grayscale_mode(
    request: Request,
    enabled: str = Form(default=""),
) -> RedirectResponse:
    """Persist the grayscale-mode checkbox to ``kv_settings`` (v0.78).

    Wraps the whole UI in ``filter: grayscale(1)`` for distraction-light
    reading. HTML checkboxes only POST a value when ticked, so an empty
    ``enabled`` field is treated as "off". The kv row is normalised to
    the literal ``"1"`` / ``"0"`` strings the Jinja global +
    ``grayscale.css`` selector consume — anything else would silently
    fall back to "off" on the next render.

    Invalidates the per-request cache so the redirect-target render
    reflects the new value rather than the value cached earlier in this
    same request when the GET form was rendered.
    """
    new_value = enabled.strip().lower() in {"1", "true", "yes", "on"}
    async with get_connection() as conn:
        await set_kv(conn, _GRAYSCALE_MODE_KV_KEY, "1" if new_value else "0")
    invalidate_grayscale_cache()
    _grayscale_log.info(
        "grayscale.toggle",
        enabled=new_value,
        source="settings_ui",
    )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/reduce-motion", response_class=HTMLResponse)
async def update_reduce_motion(
    request: Request,
    enabled: str = Form(default=""),
) -> RedirectResponse:
    """Persist the reduce-motion checkbox to ``kv_settings`` (v0.93).

    Disables every CSS transition and animation globally for users
    sensitive to motion. HTML checkboxes only POST a value when ticked,
    so an empty ``enabled`` field is treated as "off". The kv row is
    normalised to the literal ``"1"`` / ``"0"`` strings the Jinja global
    + ``reduce_motion.css`` selector consume — anything else would
    silently fall back to "off" on the next render.

    Invalidates the per-request cache so the redirect-target render
    reflects the new value rather than the value cached earlier in this
    same request when the GET form was rendered.
    """
    new_value = enabled.strip().lower() in {"1", "true", "yes", "on"}
    async with get_connection() as conn:
        await set_kv(conn, _REDUCE_MOTION_KV_KEY, "1" if new_value else "0")
    invalidate_reduce_motion_cache()
    _reduce_motion_log.info(
        "reduce_motion.toggle",
        enabled=new_value,
        source="settings_ui",
    )
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/ui-language", response_class=HTMLResponse)
async def update_ui_language(
    request: Request,
    language: str = Form(...),
) -> RedirectResponse:
    """Persist the UI language selector to ``kv_settings`` (v1.1).

    Switches the locale that drives the Jinja ``t(key)`` global wired up
    in :mod:`app.web.templates_engine`. The submitted value is checked
    against :data:`app.i18n.SUPPORTED_LANGUAGES` before writing — an
    unknown code collapses to :data:`app.i18n.DEFAULT_LANGUAGE` rather
    than being stored verbatim, so a hand-crafted POST cannot wedge the
    renderer with a missing translation file.

    Invalidates the per-request language cache so the redirect-target
    render reflects the new value rather than the value cached earlier
    in this same request when the GET form was rendered.
    """
    candidate = language.strip().lower()
    new_value = candidate if candidate in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    async with get_connection() as conn:
        await set_kv(conn, UI_LANGUAGE_KV_KEY, new_value)
    invalidate_language_cache()
    _i18n_log.info(
        "i18n.language.set",
        language=new_value,
        requested=candidate,
        source="settings_ui",
    )
    return RedirectResponse(url="/settings", status_code=303)
