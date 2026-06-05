"""Scoped read-only API tokens for third-party integrations (v1.40).

This is the *admin* surface of the bearer-token system: a label-first
issuance flow with hard expiry and a usage counter, intended for vendors
who want to query Persona without going through the operator's browser
session. It lives alongside the v0.34 :mod:`app.api_tokens` module, which
covers the internal scope-aware bearer auth Persona's own routes use;
both modules share the same ``api_token`` table (extended in migration
``140_api_tokens.sql``) and the same SHA-256 hashing rule, so a token
minted by either path validates against the other.

Design choices worth flagging
-----------------------------
* **Raw value returned exactly once.** :func:`issue_token` is the only
  function that ever sees the plaintext; the caller is responsible for
  showing it to the operator and then forgetting it. Persona persists
  only ``hashlib.sha256(...).hexdigest()`` so a DB leak reveals *that*
  tokens exist, not *what* they are.
* **Hard expiry is opt-in.** ``expires_at=None`` means "never expires";
  any non-NULL value is compared lexicographically against
  ``datetime('now')`` (both are ISO-8601 in UTC, so string order matches
  chronological order). Revoked tokens always lose, even before expiry.
* **Soft revocation, not delete.** :func:`revoke_token` sets
  ``revoked_at`` so the admin table can keep the audit row visible after
  the token stops working. We never delete rows from this table.
* **Never log plaintext.** Every log line in this module references the
  token by its DB id and label; the raw value is held in local scope for
  the duration of :func:`issue_token` and then discarded.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.api_tokens")

# 32 bytes of entropy → ~43-char urlsafe string. Matches v0.34's
# :func:`app.api_tokens.generate_raw_token` so a token minted via either
# path lands on the same hash and the two helper sets stay
# interchangeable for verification.
_TOKEN_BYTES = 32

_DEFAULT_SCOPES = "read"


class IssuedToken(TypedDict):
    """Shape returned by :func:`issue_token`. ``token_plain`` is one-shot."""

    token_plain: str
    token_id: int
    label: str
    expires_at: str | None


class TokenInfo(TypedDict):
    """Public projection of an ``api_token`` row — never includes the hash."""

    id: int
    label: str
    scopes: str
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None
    use_count: int


class VerifyOk(TypedDict):
    """Successful :func:`verify_token` result."""

    valid: bool
    token_id: int
    scopes: str
    label: str


class VerifyFail(TypedDict):
    """Failed :func:`verify_token` result with a machine-readable reason."""

    valid: bool
    reason: str


def _hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of ``raw``.

    Identical to v0.34's hashing rule so a token minted via either
    helper set validates against the other. We do not salt — the raw
    value already carries 256 bits of entropy and salting would only
    break the equality lookup.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _generate_raw_token() -> str:
    """Return a fresh 32-byte urlsafe token string."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


async def issue_token(
    label: str,
    scopes: str = _DEFAULT_SCOPES,
    expires_at: str | None = None,
) -> IssuedToken:
    """Mint a new token and return the raw value **exactly once**.

    ``label`` is the human-readable name the operator types into the
    admin form (e.g. ``"grafana-readonly"``). It must be non-empty.

    ``scopes`` is the same comma-separated string used by v0.34; the
    default of ``"read"`` is conservative on purpose — the whole point of
    this admin flow is read-only sharing with third parties.

    ``expires_at`` is either ``None`` (never expires) or an ISO-8601 UTC
    timestamp. We do not parse it here; callers (the admin form) build
    it from ``days_valid`` and pass the formatted string straight
    through.
    """
    cleaned_label = label.strip()
    if not cleaned_label:
        msg = "token label must not be empty"
        raise ValueError(msg)
    cleaned_scopes = scopes.strip() or _DEFAULT_SCOPES

    raw = _generate_raw_token()
    digest = _hash_token(raw)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO api_token (token_hash, label, scopes, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (digest, cleaned_label, cleaned_scopes, expires_at),
        )
        await conn.commit()
        new_id = cursor.lastrowid
    if new_id is None:
        # SQLite should always populate lastrowid for AUTOINCREMENT inserts;
        # bail loudly rather than carrying a None through the typed dict.
        msg = "INSERT returned no rowid"
        raise RuntimeError(msg)
    log.info(
        "api_token.issued",
        token_id=new_id,
        label=cleaned_label,
        scopes=cleaned_scopes,
        expires_at=expires_at,
    )
    # Raw value escapes this function exactly once; the caller must
    # render it to the operator and discard it.
    return IssuedToken(
        token_plain=raw,
        token_id=int(new_id),
        label=cleaned_label,
        expires_at=expires_at,
    )


async def verify_token(token_plain: str) -> VerifyOk | VerifyFail:
    """Resolve a raw token to its row, or explain the rejection.

    Returns one of:

    * ``{"valid": True, "token_id": ..., "scopes": ..., "label": ...}``
    * ``{"valid": False, "reason": "..."}`` where ``reason`` is one of
      ``empty``, ``unknown``, ``revoked``, ``expired``.

    Never logs ``token_plain``; failures log the reason only so
    ops can grep for ``api_token.verify_failed`` without ever seeing
    a plaintext token in the audit trail.
    """
    if not token_plain:
        return VerifyFail(valid=False, reason="empty")

    digest = _hash_token(token_plain)
    async with get_connection() as conn:
        row = await _fetch_by_hash(conn, digest)
    if row is None:
        log.info("api_token.verify_failed", reason="unknown")
        return VerifyFail(valid=False, reason="unknown")

    token_id = int(row["id"])
    if row["revoked_at"] is not None:
        log.info("api_token.verify_failed", token_id=token_id, reason="revoked")
        return VerifyFail(valid=False, reason="revoked")

    expires_at = row["expires_at"]
    if expires_at is not None and not await _is_still_valid(str(expires_at)):
        log.info("api_token.verify_failed", token_id=token_id, reason="expired")
        return VerifyFail(valid=False, reason="expired")

    return VerifyOk(
        valid=True,
        token_id=token_id,
        scopes=str(row["scopes"]),
        label=str(row["label"] or ""),
    )


async def list_tokens() -> list[TokenInfo]:
    """Return every token row, newest first, with the hash stripped out.

    Revoked rows are included so the UI can render them as crossed-out
    audit history; the caller decides how to display them. The hash is
    never returned — leaking it into a template would be just as bad as
    leaking the raw value itself.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, label, scopes, created_at, expires_at, "
            "revoked_at, last_used_at, use_count FROM api_token "
            "ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
    return [
        TokenInfo(
            id=int(row["id"]),
            label=str(row["label"] or ""),
            scopes=str(row["scopes"]),
            created_at=str(row["created_at"]),
            expires_at=(None if row["expires_at"] is None else str(row["expires_at"])),
            revoked_at=(None if row["revoked_at"] is None else str(row["revoked_at"])),
            last_used_at=(
                None if row["last_used_at"] is None else str(row["last_used_at"])
            ),
            use_count=int(row["use_count"] or 0),
        )
        for row in rows
    ]


async def revoke_token(token_id: int) -> None:
    """Soft-revoke a token by setting ``revoked_at = datetime('now')``.

    No-ops on already-revoked or unknown ids — the admin UI treats both
    as "nothing to do" without distinguishing, so we don't surface a
    return value here. A subsequent :func:`verify_token` call on the same
    raw value will return ``{"valid": False, "reason": "revoked"}``.
    """
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE api_token SET revoked_at = datetime('now') "
            "WHERE id = ? AND revoked_at IS NULL",
            (token_id,),
        )
        await conn.commit()
    log.info("api_token.revoked", token_id=token_id)


async def record_token_use(token_id: int) -> None:
    """Bump ``last_used_at`` + ``use_count`` for a successful request.

    Called from the FastAPI dependency after the token has already
    cleared :func:`verify_token`; we deliberately do *not* roll this into
    ``verify_token`` itself because some call sites (e.g. probing whether
    a leaked token is still valid in a test) want a side-effect-free
    check.
    """
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE api_token SET last_used_at = datetime('now'), "
            "use_count = use_count + 1 WHERE id = ?",
            (token_id,),
        )
        await conn.commit()


async def _fetch_by_hash(
    conn: aiosqlite.Connection, digest: str
) -> aiosqlite.Row | None:
    """Look up an ``api_token`` row by its SHA-256 hash."""
    cursor = await conn.execute(
        "SELECT id, label, scopes, expires_at, revoked_at "
        "FROM api_token WHERE token_hash = ?",
        (digest,),
    )
    return await cursor.fetchone()


async def _is_still_valid(expires_at: str) -> bool:
    """Return ``True`` if ``expires_at`` is in the future.

    We let SQLite do the comparison so the timezone semantics line up
    with what ``datetime('now')`` produces elsewhere in this module; a
    pure-Python ``datetime.fromisoformat`` round-trip would drift if the
    DB clock and the worker clock disagreed.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT CASE WHEN ? > datetime('now') THEN 1 ELSE 0 END AS live",
            (expires_at,),
        )
        row = await cursor.fetchone()
    if row is None:
        return False
    return bool(int(row["live"]))
