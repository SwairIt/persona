"""Session token issuance and verification backed by the ``auth_session`` table.

Tokens are opaque random 32-byte strings (hex-encoded → 64 chars). They
live in an HTTP-only cookie named ``persona_session``. The DB row is the
source of truth — there is no signed-cookie format. This keeps the model
simple and lets us revoke any session by stamping ``revoked_at``.

Default session lifetime is 30 days. Re-issuing is cheap (single INSERT)
so the route handler may shorten or extend per call.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.auth.sessions")

SESSION_COOKIE_NAME = "persona_session"
_TOKEN_BYTES = 32
_DEFAULT_LIFETIME = timedelta(days=30)


class SessionRecord(TypedDict):
    """Minimal projection of an auth_session row we hand back to callers."""

    token: str
    user_id: int
    expires_at: str
    email: str
    display_name: str | None


async def issue_session(
    user_id: int,
    *,
    user_agent: str | None = None,
    lifetime: timedelta | None = None,
) -> tuple[str, datetime]:
    """Create a new session row and return ``(token, expires_at)``.

    The caller is responsible for setting the cookie on the response.
    """
    token = secrets.token_hex(_TOKEN_BYTES)
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + (lifetime or _DEFAULT_LIFETIME)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO auth_session "
            "(token, user_id, issued_at, expires_at, user_agent, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                token,
                user_id,
                issued_at.isoformat(),
                expires_at.isoformat(),
                user_agent,
                issued_at.isoformat(),
            ),
        )
        await conn.commit()
    log.info("auth.session.issued", user_id=user_id, expires_at=expires_at.isoformat())
    return token, expires_at


async def verify_session(token: str | None) -> SessionRecord | None:
    """Look up ``token`` and return the session record when active.

    Returns ``None`` for missing / unknown / revoked / expired tokens.
    Updates ``last_seen_at`` as a side effect so we can later show
    "last active 2 minutes ago" per session.
    """
    if not token:
        return None
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT s.token, s.user_id, s.expires_at, s.revoked_at, "
            "       u.email, u.display_name "
            "FROM auth_session s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?",
            (token,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        if str(row["expires_at"]) < now:
            return None
        # Fire-and-forget last_seen bump; failure must not break auth.
        try:
            await conn.execute(
                "UPDATE auth_session SET last_seen_at = ? WHERE token = ?",
                (now, token),
            )
            await conn.commit()
        except Exception as exc:
            log.debug("auth.session.last_seen_update_failed", error=str(exc))
    return {
        "token": str(row["token"]),
        "user_id": int(row["user_id"]),
        "expires_at": str(row["expires_at"]),
        "email": str(row["email"]),
        "display_name": (
            str(row["display_name"]) if row["display_name"] is not None else None
        ),
    }


async def revoke_session(token: str) -> None:
    """Mark the session as revoked. Idempotent."""
    if not token:
        return
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE auth_session SET revoked_at = ? "
            "WHERE token = ? AND revoked_at IS NULL",
            (now, token),
        )
        await conn.commit()
    log.info("auth.session.revoked")


async def count_active_non_owner_sessions(owner_user_id: int) -> int:
    """Count active sessions that do not belong to the primary owner."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM auth_session "
            "WHERE user_id <> ? AND revoked_at IS NULL",
            (int(owner_user_id),),
        )
        row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def revoke_non_owner_sessions(owner_user_id: int) -> int:
    """Revoke every active non-owner session without deleting user accounts.

    The update is idempotent: already-revoked rows stay untouched and repeated
    invocations return zero once the lockdown has been applied.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE auth_session SET revoked_at = ? "
            "WHERE user_id <> ? AND revoked_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), int(owner_user_id)),
        )
        await conn.commit()
        changed = int(cursor.rowcount or 0)
    log.warning(
        "auth.sessions.non_owner_revoked",
        owner_user_id=int(owner_user_id),
        revoked=changed,
    )
    return changed
