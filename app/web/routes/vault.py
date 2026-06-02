"""HTTP API for the encrypted key/value vault (v0.33).

The KV vault stores BYO API keys (Anthropic / OpenAI / Groq), webhook
secrets and SMTP passwords under a master password the user re-enters
on every read. The page at ``GET /vault`` lists stored key *names*
only — values are never rendered until the user posts the password
through ``POST /vault/get`` and even then the value comes back inside
a single page that displays it once and is not cached.

The legacy private-vault screenshot endpoints (``/api/screenshots/{id}
/{make-private,restore-public,unlock,unlock-thumbnail}``) are kept on
the same router so :file:`app/web/main.py` continues to import a single
``vault_routes.router`` without changes. Those endpoints encrypt
screenshot OCR + thumbnails — unrelated machinery in :mod:`app.storage.
vault` — and are wholly independent from the new KV vault.
"""

from __future__ import annotations

import io

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.audit import log_action
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.storage.vault import (
    VaultError,
    make_private,
    restore_to_public,
    unlock,
)
from app.vault import delete_secret, get_secret, list_keys, set_secret
from app.web.templates_engine import templates

router = APIRouter(tags=["vault"])


# ---------------------------------------------------------------------------
# Encrypted key/value vault (v0.33)
# ---------------------------------------------------------------------------


def _render_vault_page(
    request: Request,
    *,
    items: list[dict[str, str]],
    revealed_key: str | None = None,
    revealed_value: str | None = None,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Common render helper — keeps every code path through one template call."""
    return templates.TemplateResponse(
        request,
        "vault.html",
        {
            "title": "Encrypted vault",
            "active_nav": "vault",
            # ``base.html`` re-uses the name ``items`` for its nav loop and
            # shadows context vars in child blocks; we namespace ours to
            # avoid the surprise tuple-vs-dict mismatch.
            "vault_items": items,
            "revealed_key": revealed_key,
            "revealed_value": revealed_value,
            "error": error,
            "notice": notice,
        },
        status_code=status_code,
    )


@router.get("/vault", response_class=HTMLResponse)
async def vault_index(request: Request) -> HTMLResponse:
    """List every stored key name (no values)."""
    items = await list_keys()
    return _render_vault_page(request, items=items)


@router.post("/vault/set", response_class=HTMLResponse)
async def vault_set(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    """Encrypt ``value`` under ``password`` and persist it as ``key``."""
    trimmed_key = key.strip()
    if not trimmed_key:
        items = await list_keys()
        await log_action(
            "vault.set",
            target="",
            detail="empty key rejected",
            success=False,
        )
        return _render_vault_page(request, items=items, error="Key name is required.")

    result = await set_secret(trimmed_key, value, password)
    items = await list_keys()
    # Audit the key name + status only. The plaintext value and master
    # password are NEVER threaded into the audit log.
    await log_action(
        "vault.set",
        target=trimmed_key,
        detail="status=" + str(result.get("status", "")),
        success=result.get("status") == "ok",
    )
    if result["status"] == "ok":
        return _render_vault_page(
            request,
            items=items,
            notice=f"Stored secret for '{trimmed_key}'.",
        )
    if result["status"] == "missing_dep":
        return _render_vault_page(request, items=items, error=str(result.get("hint", "")))
    return _render_vault_page(
        request,
        items=items,
        error=str(result.get("error", "Unable to store secret.")),
    )


@router.post("/vault/get", response_class=HTMLResponse)
async def vault_get(
    request: Request,
    key: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    """Decrypt and reveal a single value, gated by the master password."""
    trimmed_key = key.strip()
    result = await get_secret(trimmed_key, password)
    items = await list_keys()
    # Audit the read attempt. ``success`` reflects whether the master
    # password unlocked the row; the plaintext value never leaves
    # ``result`` for the log.
    await log_action(
        "vault.get",
        target=trimmed_key,
        detail="status=" + str(result.get("status", "")),
        success=result.get("status") == "ok",
    )

    if result["status"] == "ok":
        return _render_vault_page(
            request,
            items=items,
            revealed_key=trimmed_key,
            revealed_value=str(result.get("value", "")),
        )
    if result["status"] == "wrong_password":
        return _render_vault_page(
            request,
            items=items,
            error="Wrong master password — value not revealed.",
            status_code=401,
        )
    if result["status"] == "not_found":
        return _render_vault_page(
            request,
            items=items,
            error=f"No secret stored for key '{trimmed_key}'.",
        )
    if result["status"] == "missing_dep":
        return _render_vault_page(request, items=items, error=str(result.get("hint", "")))
    return _render_vault_page(
        request,
        items=items,
        error=str(result.get("error", "Unable to read secret.")),
    )


@router.post("/vault/{key}/delete")
async def vault_delete(key: str) -> RedirectResponse:
    """Drop a stored secret (no password gate — see module docstring rationale)."""
    result = await delete_secret(key)
    await log_action(
        "vault.delete",
        target=key,
        detail="status=" + str(result.get("status", "")),
        success=result.get("status") == "ok",
    )
    return RedirectResponse(url="/vault", status_code=303)


# ---------------------------------------------------------------------------
# Legacy private-vault screenshot endpoints (preserved from v0.11)
# ---------------------------------------------------------------------------


@router.post("/api/screenshots/{screenshot_id}/make-private", response_class=JSONResponse)
async def make_private_endpoint(
    screenshot_id: int,
    passphrase: str = Form(...),
) -> JSONResponse:
    async with get_connection() as conn:
        if (await get_screenshot(conn, screenshot_id)) is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        try:
            await make_private(conn, screenshot_id=screenshot_id, passphrase=passphrase)
        except VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"screenshot_id": screenshot_id, "is_private": True})


@router.post("/api/screenshots/{screenshot_id}/restore-public", response_class=JSONResponse)
async def restore_endpoint(
    screenshot_id: int,
    passphrase: str = Form(...),
) -> JSONResponse:
    async with get_connection() as conn:
        try:
            await restore_to_public(conn, screenshot_id=screenshot_id, passphrase=passphrase)
        except VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"screenshot_id": screenshot_id, "is_private": False})


@router.post("/api/screenshots/{screenshot_id}/unlock", response_class=JSONResponse)
async def unlock_endpoint(
    screenshot_id: int,
    passphrase: str = Form(...),
) -> JSONResponse:
    async with get_connection() as conn:
        try:
            unlocked = await unlock(conn, screenshot_id=screenshot_id, passphrase=passphrase)
        except VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "screenshot_id": screenshot_id,
            "ocr_text": unlocked.ocr_text,
            "has_thumbnail": unlocked.thumbnail_bytes is not None,
        }
    )


@router.post("/api/screenshots/{screenshot_id}/unlock-thumbnail")
async def unlock_thumbnail_endpoint(
    screenshot_id: int,
    passphrase: str = Form(...),
) -> StreamingResponse:
    async with get_connection() as conn:
        try:
            unlocked = await unlock(conn, screenshot_id=screenshot_id, passphrase=passphrase)
        except VaultError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not unlocked.thumbnail_bytes:
        raise HTTPException(status_code=404, detail="No thumbnail in vault entry")
    return StreamingResponse(
        io.BytesIO(unlocked.thumbnail_bytes),
        media_type="image/webp",
        headers={"Cache-Control": "no-store"},
    )
