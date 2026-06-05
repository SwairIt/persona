"""Admin UI + JSON endpoints for scoped read-only API tokens (v1.40).

Distinct from the v0.34 :mod:`app.web.routes.api_tokens` settings page —
that one mints internal tokens with scope checkboxes; this one is the
*third-party* surface: a label-first issuance form with hard expiry, a
JSON listing endpoint, and a one-shot reveal page. The two pages live
side by side in the navigation; both write to the same ``api_token``
table so the operator sees every issued token in either list.

Routes
------
``GET  /admin/api-tokens``                — admin table + issue form
``POST /admin/api-tokens/issue``          — mint, redirect to reveal page
``POST /admin/api-tokens/{id}/revoke``    — soft revoke, redirect to list
``GET  /api/api-tokens.json``             — JSON list (no hashes ever)
``GET  /api/v1/screenshots.json``         — example Bearer-protected route

This module deliberately *does not* edit :file:`app/web/main.py`; the
router is exported as :data:`router` so a future wiring change can pick
it up via the standard ``app.include_router(...)`` pattern without
touching the v0.34 wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.api_token_middleware import get_token_owner
from app.api_tokens_admin import (
    issue_token,
    list_tokens,
    revoke_token,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from app.api_tokens_admin import VerifyOk

router = APIRouter(tags=["api-tokens-admin"])
log = get_logger("persona.api_tokens")

# Recent-shots cap on the example consumer endpoint. 100 keeps the
# response well under a megabyte even with long OCR strings, and matches
# the ``/api/screenshots/recent`` legacy cap so a client switching to the
# new auth flow sees the same payload size.
_RECENT_SHOT_LIMIT = 100


def _format_expires_at(days_valid: int | None) -> str | None:
    """Convert ``days_valid`` (from the form) into an ISO-8601 UTC string.

    ``None`` or ``<= 0`` means "never expires" — we hand back ``None`` so
    the DB row stores NULL and :func:`verify_token` skips the expiry
    check. Anything positive becomes ``now + days`` in UTC, matching what
    ``datetime('now')`` produces inside SQLite so string comparison stays
    chronologically correct.
    """
    if days_valid is None or days_valid <= 0:
        return None
    expires = datetime.now(UTC) + timedelta(days=days_valid)
    # Mirror SQLite's ``datetime('now')`` format: ``YYYY-MM-DD HH:MM:SS``
    # with no timezone suffix, both expressed in UTC.
    return expires.strftime("%Y-%m-%d %H:%M:%S")


@router.get("/admin/api-tokens", response_class=HTMLResponse)
async def admin_api_tokens_page(request: Request) -> HTMLResponse:
    """Render the admin table of issued tokens plus the issue form.

    Unlike the v0.34 settings page this view never receives the raw
    token via a query parameter — the reveal is a separate POST→GET
    handshake so a browser refresh of the list page can never re-leak a
    plaintext value through the URL bar.
    """
    tokens = await list_tokens()
    return templates.TemplateResponse(
        request,
        "api_tokens_admin.html",
        {
            "title": "API токены",
            "active_nav": "settings",
            "tokens": tokens,
        },
    )


@router.post("/admin/api-tokens/issue", response_model=None)
async def admin_api_tokens_issue(
    request: Request,
    label: str = Form(...),
    scopes: str = Form("read"),
    days_valid: int = Form(0),
) -> HTMLResponse | RedirectResponse:
    """Mint a new token and render the one-shot reveal page.

    The plaintext token is returned to the browser inline rather than
    via a redirect query parameter — that's the whole point of the
    separate ``api_token_issued.html`` template. The operator sees the
    raw value once, copies it, and clicks back to the list; refreshing
    the reveal page bounces them to the list (we don't re-issue).

    ``days_valid <= 0`` is treated as "never expires" so a curl user
    can omit the field entirely without accidentally minting a
    zero-second token.
    """
    cleaned_label = label.strip()
    if not cleaned_label:
        # Validation failure → bounce back to the form. The browser's
        # bfcache preserves the partially-filled form so the operator
        # can fix the label without re-entering scopes/days.
        log.info("api_token.admin.issue_failed", reason="empty_label")
        return RedirectResponse(url="/admin/api-tokens", status_code=303)

    expires_at = _format_expires_at(days_valid)
    issued = await issue_token(
        label=cleaned_label,
        scopes=scopes.strip() or "read",
        expires_at=expires_at,
    )
    # Never log the raw value. The structured event only carries the
    # id + label + expiry so the audit trail is greppable but plaintext-
    # free.
    log.info(
        "api_token.admin.issued",
        token_id=issued["token_id"],
        label=issued["label"],
        expires_at=issued["expires_at"],
    )
    return templates.TemplateResponse(
        request,
        "api_token_issued.html",
        {
            "title": "Новый API токен",
            "active_nav": "settings",
            "issued": issued,
        },
    )


@router.post("/admin/api-tokens/{token_id}/revoke")
async def admin_api_tokens_revoke(token_id: int) -> RedirectResponse:
    """Soft-revoke a token (sets ``revoked_at``) and return to the list."""
    await revoke_token(token_id)
    return RedirectResponse(url="/admin/api-tokens", status_code=303)


@router.get("/api/api-tokens.json", response_class=JSONResponse)
async def admin_api_tokens_json() -> JSONResponse:
    """Return every token as JSON. The SHA-256 hash is **never** exposed.

    Useful for dashboards / monitoring tools that want to inventory
    Persona's issued tokens without scraping the HTML admin page. The
    field list mirrors :class:`TokenInfo` exactly so a consumer can
    pickle the response straight into a type-checked struct.
    """
    tokens = await list_tokens()
    return JSONResponse({"tokens": tokens})


@router.get("/api/v1/screenshots.json", response_class=JSONResponse)
async def example_screenshots(
    owner: VerifyOk = Depends(get_token_owner),  # noqa: B008 — FastAPI DI pattern
) -> JSONResponse:
    """Recent screenshots — example consumer of :func:`get_token_owner`.

    Proves the new bearer-auth dependency works end-to-end without
    modifying any existing route. A third-party tool only needs:

    .. code-block:: bash

        curl -H "Authorization: Bearer $TOKEN" \\
             http://127.0.0.1:8765/api/v1/screenshots.json

    Returns the most recent :data:`_RECENT_SHOT_LIMIT` rows with the
    fields a vendor dashboard typically wants — id, capture time, app,
    window title, OCR snippet. Heavy fields (full OCR text, OCR words)
    are intentionally omitted to keep the payload small.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title, "
            "substr(ocr_text, 1, 200) AS ocr_snippet "
            "FROM screenshots ORDER BY captured_at DESC LIMIT ?",
            (_RECENT_SHOT_LIMIT,),
        )
        rows = await cursor.fetchall()
    shots = [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "app_name": (None if row["app_name"] is None else str(row["app_name"])),
            "window_title": (
                None if row["window_title"] is None else str(row["window_title"])
            ),
            "ocr_snippet": (
                None if row["ocr_snippet"] is None else str(row["ocr_snippet"])
            ),
        }
        for row in rows
    ]
    return JSONResponse(
        {
            "token_id": owner["token_id"],
            "token_label": owner["label"],
            "count": len(shots),
            "shots": shots,
        }
    )
