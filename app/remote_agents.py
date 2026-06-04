"""Remote agent bearer tokens — issue, list, revoke, verify (v1.12).

External capture agents (a small uploader running on a Mac, an iOS
shortcut, a secondary laptop, ...) authenticate to Persona's
``/api/agent/*`` endpoints with an ``Authorization: Bearer <token>``
header. Tokens are minted via :mod:`app.web.routes.agents_admin`; the
**raw** value is shown exactly once and only the SHA-256 hex digest is
persisted (``remote_agent`` table, migration 094).

Design choices worth flagging
-----------------------------
* **No reversible storage.** A leaked DB row tells an attacker *that*
  an agent exists, not *what* its token is. Recovery from a lost token
  is always "revoke + re-issue", never "decrypt".
* **Constant-time comparison.** :func:`verify_agent_token` looks up the
  hash by equality (SQLite ``=``) and *also* runs
  :func:`hmac.compare_digest` before returning success, so any future
  code path that pulls the row by some other key cannot be
  timing-pivoted.
* **Revocation is soft.** ``revoked_at`` is set rather than deleting
  the row so the audit trail (name / platform / created_at /
  last_seen_at) survives.
* **Never log the raw token.** :func:`create_agent` logs the row id
  and the *name* + *platform* only. The raw string returned to the
  caller must never end up in structlog, exception messages, or HTML
  attributes.

This module is intentionally separate from :mod:`app.api_tokens`
because the threat models are different: ``api_token`` rows authorise a
human-driven CLI / browser-extension hitting ``/api/*`` with narrow
scope strings (read / write:tags / ...); ``remote_agent`` rows
authorise a long-running background uploader with a fixed,
agent-specific surface (``/api/agent/audio-segment`` and
``/api/agent/screenshot`` plus the heartbeat / self-introspect
endpoints). Keeping them apart avoids one accidental scope merge that
would turn every API token into an upload credential.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING, Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.remote_agent")

# 32 bytes of entropy → ~43-char urlsafe string. Comfortably above the
# 128-bit threshold and short enough to paste into a curl one-liner or a
# launchd plist on the agent side.
_TOKEN_BYTES = 32

# Which "kind" of upload a verified agent just performed. The
# corresponding ``last_*_at`` column is updated by
# :func:`bump_last_seen` so the admin UI can show a per-channel
# liveness column.
BumpKind = Literal["audio", "screen", "any"]

_KIND_COLUMNS: dict[BumpKind, tuple[str, ...]] = {
    "audio": ("last_seen_at", "last_audio_at"),
    "screen": ("last_seen_at", "last_screen_at"),
    "any": ("last_seen_at",),
}


class AgentInfo(TypedDict):
    """Public projection of a row in ``remote_agent`` — never includes the hash."""

    id: int
    name: str
    platform: str | None
    created_at: str
    last_seen_at: str | None
    last_audio_at: str | None
    last_screen_at: str | None
    revoked_at: str | None


class VerifiedAgent(TypedDict):
    """Subset of an agent row a verified caller is allowed to see."""

    id: int
    name: str
    platform: str | None
    last_seen_at: str | None
    last_audio_at: str | None
    last_screen_at: str | None


def _generate_raw_token() -> str:
    """Return a fresh 32-byte urlsafe token string.

    Uses :func:`secrets.token_urlsafe` so the value is safe for shells,
    URLs and HTTP headers without further encoding.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def _hash_token(raw: str) -> str:
    """Return the SHA-256 hex digest of ``raw``.

    Deterministic so a re-presented token always lands on the same DB
    row; we never salt because the raw value itself already carries 256
    bits of entropy — adding a salt would only break lookup.
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_agent(name: str, platform: str | None = None) -> tuple[int, str]:
    """Mint a new agent row and return ``(agent_id, raw_token)``.

    The caller is responsible for showing the raw string to the
    operator immediately and then discarding it — Persona itself only
    ever sees the hash again after this function returns. The audit
    log entry stamped by the route handler must reference the agent
    *id* and *name* only, never the raw token.
    """
    cleaned_name = name.strip()
    if not cleaned_name:
        msg = "agent name must not be empty"
        raise ValueError(msg)

    cleaned_platform: str | None = None
    if platform is not None:
        stripped = platform.strip()
        cleaned_platform = stripped or None

    raw = _generate_raw_token()
    digest = _hash_token(raw)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO remote_agent (name, token_hash, platform) VALUES (?, ?, ?)",
            (cleaned_name, digest, cleaned_platform),
        )
        await conn.commit()
        new_id = cursor.lastrowid
    if new_id is None:
        # Defensive: aiosqlite always populates lastrowid on a successful
        # INSERT, but the static return type allows None so we surface a
        # loud error rather than silently handing the caller a bogus 0.
        msg = "INSERT did not return a row id"
        raise RuntimeError(msg)

    log.info(
        "remote_agent.created",
        agent_id=int(new_id),
        name=cleaned_name,
        platform=cleaned_platform,
    )
    # Returns the raw token exactly once. The caller MUST NOT log it.
    return int(new_id), raw


async def verify_agent_token(raw: str) -> VerifiedAgent | None:
    """Resolve a raw bearer token to its agent row, or return ``None``.

    Updates ``last_seen_at`` is intentionally NOT performed here — the
    route helper calls :func:`bump_last_seen` afterwards so we can
    record which channel (audio vs screen vs plain heartbeat) the
    request belonged to.

    Never logs ``raw``; on failure we log a coarse reason
    (``unknown`` / ``revoked``) and the prefix-free match count only.
    """
    if not raw:
        return None

    digest = _hash_token(raw)
    async with get_connection() as conn:
        row = await _fetch_by_hash(conn, digest)

    if row is None:
        log.info("remote_agent.verify_failed", reason="unknown")
        return None

    # Defence-in-depth: hash equality already came from SQLite's ``=``
    # but we still want a constant-time comparison so any future code
    # path that pulls the row by some other key can't be timing-
    # pivoted into a digest oracle.
    stored_hash = str(row["token_hash"])
    if not hmac.compare_digest(stored_hash, digest):
        log.info("remote_agent.verify_failed", reason="hash_mismatch")
        return None

    if row["revoked_at"] is not None:
        log.info(
            "remote_agent.verify_failed",
            agent_id=int(row["id"]),
            reason="revoked",
        )
        return None

    return VerifiedAgent(
        id=int(row["id"]),
        name=str(row["name"]),
        platform=(None if row["platform"] is None else str(row["platform"])),
        last_seen_at=(None if row["last_seen_at"] is None else str(row["last_seen_at"])),
        last_audio_at=(
            None if row["last_audio_at"] is None else str(row["last_audio_at"])
        ),
        last_screen_at=(
            None if row["last_screen_at"] is None else str(row["last_screen_at"])
        ),
    )


async def bump_last_seen(agent_id: int, kind: BumpKind = "any") -> None:
    """Refresh the liveness timestamps for ``agent_id``.

    ``kind`` selects which secondary column gets touched alongside the
    always-updated ``last_seen_at``:

    * ``"audio"`` — also updates ``last_audio_at``.
    * ``"screen"`` — also updates ``last_screen_at``.
    * ``"any"`` (default) — only updates ``last_seen_at``; used by
      ``/heartbeat`` and ``/me`` where the channel is ambiguous.

    Never raises — a failure here logs a structured warning and
    returns silently so a missed bump cannot 500 the upload itself.
    """
    columns = _KIND_COLUMNS.get(kind, _KIND_COLUMNS["any"])
    assignments = ", ".join(f"{col} = datetime('now')" for col in columns)
    sql = f"UPDATE remote_agent SET {assignments} WHERE id = ?"  # noqa: S608 — column names from a frozen whitelist
    try:
        async with get_connection() as conn:
            await conn.execute(sql, (int(agent_id),))
            await conn.commit()
    except Exception as exc:
        log.warning(
            "remote_agent.bump_failed",
            agent_id=int(agent_id),
            kind=kind,
            error=str(exc),
        )


async def list_agents(*, include_revoked: bool = False) -> list[AgentInfo]:
    """Return every agent row, newest first, with the hash stripped out.

    Revoked rows are excluded by default so the admin table is not
    dominated by dead history; pass ``include_revoked=True`` to surface
    the full audit trail (the template renders revoked rows crossed-out).
    """
    if include_revoked:
        sql = (
            "SELECT id, name, platform, created_at, last_seen_at, "
            "last_audio_at, last_screen_at, revoked_at "
            "FROM remote_agent ORDER BY id DESC"
        )
    else:
        sql = (
            "SELECT id, name, platform, created_at, last_seen_at, "
            "last_audio_at, last_screen_at, revoked_at "
            "FROM remote_agent WHERE revoked_at IS NULL ORDER BY id DESC"
        )
    async with get_connection() as conn:
        cursor = await conn.execute(sql)
        rows = await cursor.fetchall()
    return [
        AgentInfo(
            id=int(row["id"]),
            name=str(row["name"]),
            platform=(None if row["platform"] is None else str(row["platform"])),
            created_at=str(row["created_at"]),
            last_seen_at=(
                None if row["last_seen_at"] is None else str(row["last_seen_at"])
            ),
            last_audio_at=(
                None if row["last_audio_at"] is None else str(row["last_audio_at"])
            ),
            last_screen_at=(
                None if row["last_screen_at"] is None else str(row["last_screen_at"])
            ),
            revoked_at=(None if row["revoked_at"] is None else str(row["revoked_at"])),
        )
        for row in rows
    ]


async def revoke_agent(agent_id: int) -> bool:
    """Mark an agent as revoked. Returns ``True`` if a live row was hit.

    No-ops on already-revoked or unknown ids (returns ``False``) — the
    caller can treat both as "nothing to do" without distinguishing.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE remote_agent SET revoked_at = datetime('now') "
            "WHERE id = ? AND revoked_at IS NULL",
            (int(agent_id),),
        )
        await conn.commit()
        changed = cursor.rowcount or 0
    if changed:
        log.info("remote_agent.revoked", agent_id=int(agent_id))
    return bool(changed)


async def _fetch_by_hash(
    conn: aiosqlite.Connection, digest: str
) -> aiosqlite.Row | None:
    cursor = await conn.execute(
        "SELECT id, name, platform, token_hash, revoked_at, "
        "last_seen_at, last_audio_at, last_screen_at "
        "FROM remote_agent WHERE token_hash = ?",
        (digest,),
    )
    return await cursor.fetchone()


__all__ = [
    "AgentInfo",
    "BumpKind",
    "VerifiedAgent",
    "bump_last_seen",
    "create_agent",
    "list_agents",
    "revoke_agent",
    "verify_agent_token",
]
