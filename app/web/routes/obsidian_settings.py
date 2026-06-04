"""HTTP surface for the Obsidian vault sync feature.

Three endpoints:

* ``GET  /settings/obsidian`` — render the settings form (vault path,
  enabled toggle, lookback days slider).
* ``POST /settings/obsidian`` — persist the form. Validates the vault
  path (absolute + existing dir + writable + not overlapping Persona
  itself) BEFORE writing kv so a bad value never silently lands in
  the database. Validation failures return HTTP 400 with the hint
  text so the user sees exactly what to fix.
* ``POST /api/obsidian/sync-now`` — fire :func:`sync_to_vault` once
  and return the result dict.

State lives in three kv_settings rows:

==========================  =================================  ===========
kv key                       form field                         shape
==========================  =================================  ===========
``obsidian_sync_enabled``    checkbox ``enabled``               ``"0"``/``"1"``
``obsidian_vault_path``      text ``vault_path``                string
``obsidian_lookback_days``   number ``lookback_days``           int (1-90)
==========================  =================================  ===========
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.obsidian_sync import (
    VaultSafetyError,
    sync_to_vault,
    validate_vault_path,
)
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

log = get_logger("persona.obsidian.web")

router = APIRouter(tags=["obsidian-settings"])

# ----- kv_settings row names ---------------------------------------------

_KV_ENABLED: Final[str] = "obsidian_sync_enabled"
"""Gate row — must match
:data:`app.workers.obsidian_sync_worker._KV_ENABLED`."""

_KV_VAULT_PATH: Final[str] = "obsidian_vault_path"
"""Vault root path — must match
:data:`app.workers.obsidian_sync_worker._KV_VAULT_PATH`."""

_KV_LOOKBACK_DAYS: Final[str] = "obsidian_lookback_days"
"""Slider value. The worker still uses its own
:data:`~app.workers.obsidian_sync_worker.LOOKBACK_DAYS` constant for
the periodic poll; this kv row only controls the "Run now" button so
the user can ask for a deeper one-shot backfill without bumping the
worker default."""

# ----- lookback bounds ---------------------------------------------------

_LOOKBACK_MIN: Final[int] = 1
_LOOKBACK_MAX: Final[int] = 90
_LOOKBACK_DEFAULT: Final[int] = 14


# ---------------------------------------------------------------------------
# kv coercion
# ---------------------------------------------------------------------------


def _parse_checkbox(value: str) -> bool:
    """Form checkbox parsing — present + non-empty → True."""
    cleaned = value.strip().lower()
    return cleaned in {"1", "on", "true", "yes"}


def _read_bool(raw: str | None) -> bool:
    return (raw or "").strip() == "1"


def _read_lookback(raw: str | None) -> int:
    if raw is None:
        return _LOOKBACK_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        return _LOOKBACK_DEFAULT
    if value < _LOOKBACK_MIN:
        return _LOOKBACK_MIN
    if value > _LOOKBACK_MAX:
        return _LOOKBACK_MAX
    return value


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/settings/obsidian", response_class=HTMLResponse)
async def obsidian_settings_page(request: Request) -> HTMLResponse:
    """Render the Obsidian-sync settings form."""
    async with get_connection() as conn:
        enabled_raw = await get_kv(conn, _KV_ENABLED)
        vault_path_raw = await get_kv(conn, _KV_VAULT_PATH)
        lookback_raw = await get_kv(conn, _KV_LOOKBACK_DAYS)

    enabled = _read_bool(enabled_raw)
    vault_path = (vault_path_raw or "").strip()
    lookback_days = _read_lookback(lookback_raw)

    log.info(
        "obsidian.settings.page",
        enabled=enabled,
        has_vault_path=bool(vault_path),
        lookback_days=lookback_days,
    )

    return templates.TemplateResponse(
        request,
        "obsidian_settings.html",
        {
            "title": "Obsidian-синхронизация",
            "active_nav": "settings",
            "enabled": enabled,
            "vault_path": vault_path,
            "lookback_days": lookback_days,
            "lookback_min": _LOOKBACK_MIN,
            "lookback_max": _LOOKBACK_MAX,
        },
    )


@router.post("/settings/obsidian", response_class=HTMLResponse)
async def obsidian_settings_save(
    request: Request,
    vault_path: str = Form(default=""),
    enabled: str = Form(default=""),
    lookback_days: str = Form(default=""),
) -> RedirectResponse:
    """Persist Obsidian-sync settings.

    Validates the vault path before writing any kv row. If the path is
    set but unsafe (relative, missing, not writable, overlaps Persona)
    we return HTTP 400 with the safety message so the user sees why
    nothing was saved.

    An empty vault path is allowed — it means "feature parked" and is
    saved as an empty kv row. The worker treats an empty path as a
    no-op tick.
    """
    vault_raw = vault_path.strip()
    enabled_value = _parse_checkbox(enabled)
    lookback_value = _read_lookback(lookback_days)

    if vault_raw:
        try:
            resolved = validate_vault_path(Path(vault_raw))
        except VaultSafetyError as exc:
            log.warning(
                "obsidian.settings.save.invalid_path",
                vault_path=vault_raw,
                error=str(exc),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        canonical = str(resolved)
    else:
        canonical = ""

    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, "1" if enabled_value else "0")
        await set_kv(conn, _KV_VAULT_PATH, canonical)
        await set_kv(conn, _KV_LOOKBACK_DAYS, str(lookback_value))

    log.info(
        "obsidian.settings.save",
        enabled=enabled_value,
        vault_path=canonical,
        lookback_days=lookback_value,
    )

    return RedirectResponse(url="/settings/obsidian", status_code=303)


@router.post("/api/obsidian/sync-now", response_class=JSONResponse)
async def obsidian_sync_now(request: Request) -> JSONResponse:
    """Fire one sync immediately and return the result dict.

    Reads the configured vault path + lookback from kv (so the button
    honours whatever the user just saved) and refuses with HTTP 400
    when the path is missing or unsafe — matching the form behaviour.
    """
    async with get_connection() as conn:
        vault_raw = await get_kv(conn, _KV_VAULT_PATH)
        lookback_raw = await get_kv(conn, _KV_LOOKBACK_DAYS)

    vault_text = (vault_raw or "").strip()
    if not vault_text:
        log.warning("obsidian.sync_now.no_path")
        raise HTTPException(
            status_code=400,
            detail="vault_path is not configured",
        )

    try:
        resolved = validate_vault_path(Path(vault_text))
    except VaultSafetyError as exc:
        log.warning(
            "obsidian.sync_now.invalid_path",
            vault_path=vault_text,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    lookback = _read_lookback(lookback_raw)
    result = await sync_to_vault(resolved, days=lookback)

    log.info(
        "obsidian.sync_now.done",
        vault_path=str(resolved),
        files_written=result["files_written"],
        files_skipped=result["files_skipped"],
        errors=len(result["errors"]),
    )
    return JSONResponse(
        {
            "ok": True,
            "vault_path": str(resolved),
            "lookback_days": lookback,
            "files_written": result["files_written"],
            "files_skipped": result["files_skipped"],
            "errors": result["errors"],
        }
    )


__all__ = ["router"]
