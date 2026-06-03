"""Per-feed share tokens — issue, list, revoke, verify (v0.85).

Persona's ``/feeds/*`` RSS / Atom endpoints have always been openly
readable to anyone who could reach the host. v0.85 adds an opt-in gate
so the operator can mint a *narrowly-scoped* shareable token (e.g.
"good for ``/feeds/tags/cooking.rss`` only") without exposing every
other feed on the host.

Design choices worth flagging
-----------------------------
* **No reversible storage.** Mirrors :mod:`app.api_tokens` — only the
  SHA-256 hex digest is persisted. A leaked DB row tells an attacker
  *that* a feed token exists, not *what* it is. Recovery from a lost
  token is "revoke + re-issue", never "decrypt".
* **Constant-time comparison.** :func:`verify_token` looks up by hash
  equality (SQLite ``=``) and *also* runs :func:`hmac.compare_digest`
  before returning success so any future code path that pulls a row
  by some other key can't be timing-pivoted on the digest.
* **Pattern is operator-supplied.** ``feed_pattern`` is whatever
  glob the operator entered in the create form, matched against the
  request path via :func:`fnmatch.fnmatchcase`. A token issued for
  ``/feeds/tags/*.rss`` validates ``/feeds/tags/cooking.rss`` but
  rejects ``/feeds/journal.rss``. The empty-string pattern is
  rejected at create time so a typo can't accidentally mint a
  catch-all.
* **Revocation is soft.** ``revoked_at`` is set rather than deleting
  the row so the audit trail (name / created_at) survives.
* **Never log the raw token.** :func:`create_token` logs the row id
  and the name only. The raw string returned to the caller must never
  end up in structlog, exception messages, or HTML attributes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.feed_tokens")

# 32 bytes of entropy → ~43-char urlsafe string. Matches the API-token
# entropy budget so the two systems are interchangeable from a
# brute-force-resistance standpoint.
_TOKEN_BYTES = 32


class FeedTokenInfo(TypedDict):
    """Public projection of a row in ``feed_token`` — never includes the hash."""

    id: int
    name: str
    feed_pattern: str
    created_at: str
    revoked_at: str | None


class FeedVerifyResult(TypedDict, total=False):
    """Return shape of :func:`verify_token`.

    ``ok`` is always present. On success the caller also receives
    ``id`` so route handlers can log which token authorised the
    request; on failure they receive ``reason`` so the gate can tag
    the 401/403 response without leaking *which* check failed to the
    client.
    """

    ok: bool
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


async def create_token(name: str, feed_pattern: str) -> str:
    """Mint a new feed-token row and return the **raw** value exactly once.

    Both ``name`` and ``feed_pattern`` are required and trimmed. An
    empty ``feed_pattern`` is rejected so a typo (e.g. submitting the
    form with the pattern field cleared) cannot accidentally mint a
    catch-all token.

    The caller is responsible for showing the raw string to the user
    immediately and then discarding it — Persona itself only ever sees
    the hash again after this function returns.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        msg = "feed-token name must not be empty"
        raise ValueError(msg)
    cleaned_pattern = feed_pattern.strip()
    if not cleaned_pattern:
        msg = "feed-token feed_pattern must not be empty"
        raise ValueError(msg)

    raw = generate_raw_token()
    digest = hash_token(raw)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO feed_token (name, token_hash, feed_pattern) VALUES (?, ?, ?)",
            (cleaned_name, digest, cleaned_pattern),
        )
        await conn.commit()
        new_id = cursor.lastrowid
    log.info(
        "feed_token.created",
        token_id=new_id,
        name=cleaned_name,
        feed_pattern=cleaned_pattern,
    )
    return raw


async def list_tokens() -> list[FeedTokenInfo]:
    """Return every feed-token row, newest first, with the hash stripped out.

    Revoked rows are included so the UI can render them as crossed-out
    audit history; the caller decides how to display them.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, feed_pattern, created_at, revoked_at "
            "FROM feed_token ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
    return [
        FeedTokenInfo(
            id=int(row["id"]),
            name=str(row["name"]),
            feed_pattern=str(row["feed_pattern"]),
            created_at=str(row["created_at"]),
            revoked_at=(None if row["revoked_at"] is None else str(row["revoked_at"])),
        )
        for row in rows
    ]


async def revoke_token(token_id: int) -> bool:
    """Mark a feed-token as revoked. Returns ``True`` if a live row was hit.

    No-ops on already-revoked or unknown ids (returns ``False``) — the
    caller can treat both as "nothing to do" without distinguishing.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE feed_token SET revoked_at = datetime('now') "
            "WHERE id = ? AND revoked_at IS NULL",
            (token_id,),
        )
        await conn.commit()
        changed = cursor.rowcount or 0
    if changed:
        log.info("feed_token.revoked", token_id=token_id)
    return bool(changed)


async def verify_token(raw: str, requested_path: str) -> FeedVerifyResult:
    """Resolve a raw token + requested path to a verdict.

    The token is accepted iff:

    1. it hashes to a known, non-revoked row, and
    2. the row's ``feed_pattern`` matches ``requested_path`` via
       :func:`fnmatch.fnmatchcase`.

    Never logs ``raw``; on failure we log the *reason* only so the
    gate can tag the 401/403 response without leaking *which* check
    failed to the client. Uses :func:`hmac.compare_digest` for the
    hash comparison so any future code path that pulls a row by some
    other key (e.g. id) can't be timing-pivoted.
    """
    if not raw or not requested_path:
        reason = "empty" if not raw else "empty_path"
        return FeedVerifyResult(ok=False, reason=reason)

    digest = hash_token(raw)
    failure_reason: str | None = None
    token_id: int | None = None

    async with get_connection() as conn:
        row = await _fetch_by_hash(conn, digest)
        if row is None:
            log.info("feed_token.verify_failed", reason="unknown")
            return FeedVerifyResult(ok=False, reason="unknown")

        # Defence-in-depth: SQLite's ``=`` already did the lookup but a
        # constant-time compare guards against any future caller that
        # might fetch the row by id and trust ``token_hash`` blindly.
        stored_hash = str(row["token_hash"])
        row_id = int(row["id"])
        pattern = str(row["feed_pattern"])

        if not hmac.compare_digest(stored_hash, digest):
            failure_reason = "hash_mismatch"
        elif row["revoked_at"] is not None:
            failure_reason = "revoked"
        elif not fnmatchcase(requested_path, pattern):
            failure_reason = "pattern_mismatch"
        else:
            token_id = row_id

    if failure_reason is not None:
        # ``hash_mismatch`` is collapsed back to ``unknown`` on the wire
        # so clients can't tell "no row" from "bad hash" — that's the
        # whole point of the defence-in-depth compare.
        client_reason = "unknown" if failure_reason == "hash_mismatch" else failure_reason
        log.info(
            "feed_token.verify_failed",
            token_id=row_id,
            reason=failure_reason,
            requested_path=requested_path,
            feed_pattern=pattern,
        )
        return FeedVerifyResult(ok=False, reason=client_reason)

    assert token_id is not None  # narrowed by the failure_reason check above
    return FeedVerifyResult(ok=True, id=token_id)


async def _fetch_by_hash(
    conn: aiosqlite.Connection, digest: str
) -> aiosqlite.Row | None:
    cursor = await conn.execute(
        "SELECT id, token_hash, feed_pattern, revoked_at FROM feed_token "
        "WHERE token_hash = ?",
        (digest,),
    )
    return await cursor.fetchone()


__all__ = [
    "FeedTokenInfo",
    "FeedVerifyResult",
    "create_token",
    "generate_raw_token",
    "hash_token",
    "list_tokens",
    "revoke_token",
    "verify_token",
]
