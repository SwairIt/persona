"""API bearer tokens — issue, list, revoke, verify (v0.34).

External clients (CLIs, browser extensions, your own scripts) authenticate
to Persona's ``/api/*`` endpoints with an ``Authorization: Bearer <token>``
header. Tokens are minted via the settings UI; the **raw** value is shown
exactly once and only the SHA-256 hex digest is persisted (``api_token``
table, migration 033).

Design choices worth flagging
-----------------------------
* **No reversible storage.** A leaked DB row tells an attacker *that* a
  token exists, not *what* it is. Recovery from a lost token is always
  "revoke + re-issue", never "decrypt".
* **Constant-time comparison.** :func:`verify_token` looks up the hash
  by equality (SQLite ``=``) and *also* runs :func:`hmac.compare_digest`
  before returning success, so the lookup cost doesn't leak the digest.
* **Revocation is soft.** ``revoked_at`` is set rather than deleting the
  row so the audit trail (name / created_at / last_used_at) survives.
* **Never log the raw token.** ``create_token`` logs the row id and the
  *name* only. The raw string returned to the caller must never end up
  in structlog, exception messages, or HTML attributes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.api_tokens")

# 32 bytes of entropy → ~43-char urlsafe string. Comfortably above the
# 128-bit threshold and short enough to paste into a curl one-liner.
_TOKEN_BYTES = 32


class TokenInfo(TypedDict):
    """Public projection of a row in ``api_token`` — never includes the hash."""

    id: int
    name: str
    scopes: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None


class VerifyResult(TypedDict, total=False):
    """Return shape of :func:`verify_token`.

    ``ok`` is always present. When ``True`` the caller also receives
    ``scopes`` and ``id``; when ``False`` it receives ``reason`` so the
    middleware can tag the 401 response without leaking *which* check
    failed to the client.
    """

    ok: bool
    scopes: str
    id: int
    reason: str


def generate_raw_token() -> str:
    """Return a fresh 32-byte urlsafe token string.

    Uses :func:`secrets.token_urlsafe` so the value is safe for shells,
    URLs and HTTP headers without further encoding.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of ``raw``.

    Deterministic so a re-presented token always lands on the same DB
    row; we never salt because the raw value itself already carries 256
    bits of entropy — adding a salt would only break lookup.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_token(name: str, scopes: str = "read") -> str:
    """Mint a new token row and return the **raw** value exactly once.

    The caller is responsible for showing the raw string to the user
    immediately and then discarding it — Persona itself only ever sees
    the hash again after this function returns.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        msg = "token name must not be empty"
        raise ValueError(msg)
    cleaned_scopes = scopes.strip() or "read"

    raw = generate_raw_token()
    digest = hash_token(raw)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO api_token (name, token_hash, scopes) VALUES (?, ?, ?)",
            (cleaned_name, digest, cleaned_scopes),
        )
        await conn.commit()
        new_id = cursor.lastrowid
    log.info(
        "api_token.created",
        token_id=new_id,
        name=cleaned_name,
        scopes=cleaned_scopes,
    )
    return raw


async def list_tokens() -> list[TokenInfo]:
    """Return every token row, newest first, with the hash stripped out.

    Revoked rows are included so the UI can render them as crossed-out
    audit history; the caller decides how to display them.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, scopes, created_at, last_used_at, revoked_at "
            "FROM api_token ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
    return [
        TokenInfo(
            id=int(row["id"]),
            name=str(row["name"]),
            scopes=str(row["scopes"]),
            created_at=str(row["created_at"]),
            last_used_at=(None if row["last_used_at"] is None else str(row["last_used_at"])),
            revoked_at=(None if row["revoked_at"] is None else str(row["revoked_at"])),
        )
        for row in rows
    ]


async def revoke_token(token_id: int) -> bool:
    """Mark a token as revoked. Returns ``True`` if a live row was hit.

    No-ops on already-revoked or unknown ids (returns ``False``) — the
    caller can treat both as "nothing to do" without distinguishing.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE api_token SET revoked_at = datetime('now') "
            "WHERE id = ? AND revoked_at IS NULL",
            (token_id,),
        )
        await conn.commit()
        changed = cursor.rowcount or 0
    if changed:
        log.info("api_token.revoked", token_id=token_id)
    return bool(changed)


async def verify_token(raw: str) -> VerifyResult:
    """Resolve a raw bearer token to its scopes, or explain the rejection.

    Updates ``last_used_at`` on a successful verify so the operator can
    see which tokens are still in use. Never logs ``raw``; on failure
    we log the *reason* and the prefix-free count of matches only.
    """
    if not raw:
        return VerifyResult(ok=False, reason="empty")

    digest = hash_token(raw)
    async with get_connection() as conn:
        row = await _fetch_by_hash(conn, digest)
        if row is None:
            log.info("api_token.verify_failed", reason="unknown")
            return VerifyResult(ok=False, reason="unknown")

        # Defence-in-depth: hash equality already came from SQLite's ``=``
        # but we still want a constant-time comparison so any future code
        # path that pulls the row by some other key can't be timing-pivoted.
        stored_hash = str(row["token_hash"])
        if not hmac.compare_digest(stored_hash, digest):
            log.info("api_token.verify_failed", reason="hash_mismatch")
            return VerifyResult(ok=False, reason="unknown")

        if row["revoked_at"] is not None:
            log.info("api_token.verify_failed", token_id=int(row["id"]), reason="revoked")
            return VerifyResult(ok=False, reason="revoked")

        token_id = int(row["id"])
        scopes = str(row["scopes"])
        await conn.execute(
            "UPDATE api_token SET last_used_at = datetime('now') WHERE id = ?",
            (token_id,),
        )
        await conn.commit()

    return VerifyResult(ok=True, scopes=scopes, id=token_id)


async def _fetch_by_hash(
    conn: aiosqlite.Connection, digest: str
) -> aiosqlite.Row | None:
    cursor = await conn.execute(
        "SELECT id, token_hash, scopes, revoked_at FROM api_token WHERE token_hash = ?",
        (digest,),
    )
    return await cursor.fetchone()
