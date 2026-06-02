"""HTTP surface for the opt-in per-note body encryption (v0.45).

Three routes, all under ``/api/notes/{id}/…``:

    * ``POST /encrypt``           — flip a plaintext note to ciphertext.
    * ``POST /decrypt``           — return the plaintext **once** in the
                                    JSON response. The body is *not*
                                    re-persisted; the caller decides
                                    whether to display it.
    * ``POST /unlock-and-edit``   — return a short-lived signed token the
                                    edit page can present to read the
                                    plaintext (via a one-shot
                                    side-channel handler not implemented
                                    here — this endpoint mints the token
                                    only; consuming it is the edit
                                    route's responsibility).

The unlock token is signed with an HMAC-SHA256 secret generated once per
process (:data:`_UNLOCK_SECRET`) and carries the note id, an issued-at
timestamp and the decrypted plaintext. It expires after
:data:`_UNLOCK_TTL_SECONDS` seconds; tokens older than that fail
verification regardless of signature validity.

Plaintext, password and ciphertext are **never** logged. Audit logging
of decrypt attempts (success and failure) is already handled inside
:mod:`app.encrypted_notes`; we log only the HTTP-level outcome here so
the audit table doesn't get duplicate rows.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``. Wire
it up in a follow-up patch with::

    from app.web.routes import encrypted_notes as encrypted_notes_routes
    app.include_router(encrypted_notes_routes.router)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import Any, Final

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.encrypted_notes import (
    BadPassword,
    decrypt_note,
    encrypt_note,
    list_encrypted,
)
from app.logging_setup import get_logger

log = get_logger("persona.encrypted_notes.routes")

router = APIRouter(prefix="/api/notes", tags=["encrypted-notes"])

# Process-local HMAC secret used to sign unlock tokens. Generated once at
# import time — restarting the process invalidates every outstanding token,
# which is exactly the behaviour we want (no replay across restarts).
_UNLOCK_SECRET: Final[bytes] = secrets.token_bytes(32)

# Unlock tokens are short-lived. Two minutes is enough to hand off to the
# edit page and submit the form once; well below "leave the laptop and
# walk away" risk.
_UNLOCK_TTL_SECONDS: Final[int] = 120


# ---------------------------------------------------------------------------
# Unlock-token signer
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    """Encode ``raw`` as URL-safe base64 with the padding stripped.

    Stripped padding keeps the token short and free of ``=`` (which
    survives in URLs but is ugly in JSON). The decoder re-adds padding
    as needed.
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Inverse of :func:`_b64url`; tolerant of the stripped padding."""
    pad = 4 - (len(s) % 4)
    if pad != 4:
        s = s + ("=" * pad)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _sign_unlock_token(note_id: int, plaintext: str) -> str:
    """Build a ``<payload>.<sig>`` token carrying the plaintext + an exp ts.

    Payload is JSON: ``{"id": <note_id>, "iat": <unix_ts>, "pt": <text>}``.
    The signature is an HMAC-SHA256 over the payload bytes. Both halves
    are URL-safe base64 so the token is safe in headers, cookies and
    URLs without further encoding.
    """
    payload = {
        "id": int(note_id),
        "iat": int(time.time()),
        "pt": plaintext,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_UNLOCK_SECRET, payload_bytes, hashlib.sha256).digest()
    return f"{_b64url(payload_bytes)}.{_b64url(sig)}"


def _split_and_verify_signature(token: str) -> bytes | None:
    """Return the payload bytes iff ``token`` parses and its signature checks.

    Pulled out so :func:`verify_unlock_token` can collapse multiple
    failure paths into a single ``if … is None`` — keeping the public
    verifier within ruff's return-statement budget without losing
    distinct error coverage.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, binascii.Error):
        return None

    expected = hmac.new(_UNLOCK_SECRET, payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    return payload_bytes


def verify_unlock_token(token: str, note_id: int) -> str | None:
    """Verify ``token`` was minted for ``note_id`` and return its plaintext.

    Returns ``None`` on any failure (bad shape, bad signature, wrong
    note id, expired). Constant-time signature comparison via
    :func:`hmac.compare_digest` keeps timing leaks from leaking which
    half of the token was wrong.

    Exposed for the future edit route — keep this signature stable so
    the downstream caller doesn't have to re-derive the token format.
    """
    payload_bytes = _split_and_verify_signature(token)
    if payload_bytes is None:
        return None

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None

    # Single combined guard collapses four would-be early returns
    # (non-dict / wrong id / expired / missing-or-non-string plaintext)
    # into one branch, which keeps ruff's PLR0911 budget happy without
    # losing any of the validation cases.
    if not isinstance(payload, dict):
        return None
    pt = payload.get("pt")
    iat_raw = payload.get("iat", 0)
    try:
        iat = int(iat_raw)
        payload_id = int(payload.get("id", -1))
    except (TypeError, ValueError):
        return None
    fresh = iat > 0 and (time.time() - iat) <= _UNLOCK_TTL_SECONDS
    if payload_id != int(note_id) or not fresh or not isinstance(pt, str):
        return None
    return pt


# ---------------------------------------------------------------------------
# Status-dict → HTTP mapping (shared by /encrypt + /decrypt)
# ---------------------------------------------------------------------------


def _raise_for_status(status: str, payload: dict[str, Any]) -> None:
    """Translate a non-OK status dict into the right :class:`HTTPException`.

    Centralised so the route handlers stay readable: every failure path
    funnels through here with a consistent JSON shape.
    """
    if status == "ok":
        return
    if status == "missing_dep":
        raise HTTPException(
            status_code=503,
            detail={"status": status, "hint": payload.get("hint", "")},
        )
    if status == "not_found":
        raise HTTPException(status_code=404, detail={"status": status})
    if status == "already_encrypted":
        raise HTTPException(status_code=409, detail={"status": status})
    if status == "invalid":
        raise HTTPException(
            status_code=400,
            detail={"status": status, "error": payload.get("error", "")},
        )
    # Defensive: surface anything new from app.encrypted_notes without a 500.
    raise HTTPException(status_code=400, detail={"status": status})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/{note_id}/encrypt", response_class=JSONResponse)
async def encrypt_route(note_id: int, password: str = Form(...)) -> JSONResponse:
    """Flip a plaintext note into its ciphertext form.

    ``password`` arrives as ``application/x-www-form-urlencoded`` — we
    intentionally do **not** accept it via query string so it can never
    end up in access logs.
    """
    result = await encrypt_note(note_id, password)
    status = str(result.get("status", "error"))
    _raise_for_status(status, result)
    log.info("encrypted_notes.routes.encrypt.ok", note_id=note_id)
    return JSONResponse(
        {
            "note_id": int(note_id),
            "status": "ok",
            "bytes": int(result.get("bytes", 0)),
        }
    )


@router.post("/{note_id}/decrypt", response_class=JSONResponse)
async def decrypt_route(note_id: int, password: str = Form(...)) -> JSONResponse:
    """Return the decrypted body in the response — **never** persisted.

    The plaintext is included in the JSON payload exactly once; it is
    the caller's responsibility to show it (and only show it). We do
    not log the plaintext; the audit log entry is written inside
    :func:`app.encrypted_notes.decrypt_note`.
    """
    try:
        plaintext = await decrypt_note(note_id, password)
    except BadPassword as exc:
        log.info("encrypted_notes.routes.decrypt.bad", note_id=note_id)
        raise HTTPException(
            status_code=403,
            detail={"status": "bad_password", "error": str(exc)},
        ) from exc

    log.info("encrypted_notes.routes.decrypt.ok", note_id=note_id)
    return JSONResponse(
        {
            "note_id": int(note_id),
            "status": "ok",
            "body": plaintext,
        }
    )


@router.post("/{note_id}/unlock-and-edit", response_class=JSONResponse)
async def unlock_and_edit_route(
    note_id: int,
    password: str = Form(...),
) -> JSONResponse:
    """Mint a short-lived signed unlock token for the edit page.

    The token carries the plaintext (so the edit form can prefill its
    textarea on next render without re-decrypting) and is HMAC-signed
    with a per-process secret. It expires after
    :data:`_UNLOCK_TTL_SECONDS` seconds. The edit handler should pass
    the token through :func:`verify_unlock_token` before trusting the
    plaintext.
    """
    try:
        plaintext = await decrypt_note(note_id, password)
    except BadPassword as exc:
        log.info("encrypted_notes.routes.unlock.bad", note_id=note_id)
        raise HTTPException(
            status_code=403,
            detail={"status": "bad_password", "error": str(exc)},
        ) from exc

    token = _sign_unlock_token(note_id, plaintext)
    log.info(
        "encrypted_notes.routes.unlock.ok",
        note_id=note_id,
        ttl=_UNLOCK_TTL_SECONDS,
    )
    # Do NOT include the plaintext in the response — the edit page is
    # expected to submit the token back to the (future) edit handler,
    # which will call :func:`verify_unlock_token` to retrieve it.
    return JSONResponse(
        {
            "note_id": int(note_id),
            "status": "ok",
            "unlock_token": token,
            "expires_in": _UNLOCK_TTL_SECONDS,
        }
    )


@router.get("/encrypted", response_class=JSONResponse)
async def list_encrypted_route() -> JSONResponse:
    """List every encrypted note's metadata (never the ciphertext bytes).

    Useful for a settings / vault page that wants to render a table of
    locked notes with last-modified timestamps. The HTTP method is GET
    because no state changes; passwords are never involved.
    """
    items = await list_encrypted()
    return JSONResponse({"items": items, "total": len(items)})


__all__ = ["router", "verify_unlock_token"]
