"""Session token issuance and verification backed by the ``auth_session`` table.

Tokens are opaque random 32-byte strings (hex-encoded → 64 chars). They
live in an HTTP-only cookie named ``persona_session``. The DB row is the
source of truth — there is no signed-cookie format. This keeps the model
simple and lets us revoke any session by stamping ``revoked_at``.

Default session lifetime is 30 days. Re-issuing is cheap (single INSERT)
so the route handler may shorten or extend per call.

Two lifetimes, not one
----------------------
A 30-day *absolute* lifetime alone means a token stolen from a shared or
abandoned browser stays valid for a month of doing nothing. Since T2 the table
already carried ``last_seen_at`` (bumped on every verify), so an **idle**
timeout costs no schema change and no extra query: :func:`verify_session`
compares ``last_seen_at`` against :data:`IDLE_TIMEOUT` in the same row it
already reads, and treats an over-idle session as dead. Default 14 days,
override with ``PERSONA_SESSION_IDLE_DAYS`` (``0`` disables the idle check and
restores the old absolute-only behaviour).

Rotation
--------
:func:`rotate_session` issues a fresh token and revokes the old one, for the
privilege-relevant events where keeping the same identifier is a session-
fixation risk: sign-in on top of an existing session, and password change.
:func:`revoke_all_for_user` is "sign out everywhere".
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from app.auth.account_state import (
    is_active_status,
    status_column_available,
    status_of_row,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.auth.sessions")

SESSION_COOKIE_NAME = "persona_session"
_TOKEN_BYTES = 32
_DEFAULT_LIFETIME = timedelta(days=30)


def _idle_timeout() -> timedelta | None:
    """Idle window from ``PERSONA_SESSION_IDLE_DAYS`` (default 14, ``0`` = off).

    Read per call rather than cached at import so tests (and an operator with a
    restart) can change it. It is a single ``os.environ`` lookup plus an int
    parse — cheaper than the DB round-trip that verify_session already does.
    Any unparseable value falls back to the 14-day default (fail secure: the
    check stays on).
    """
    raw = os.environ.get("PERSONA_SESSION_IDLE_DAYS", "").strip()
    if not raw:
        return timedelta(days=14)
    try:
        days = float(raw)
    except ValueError:
        return timedelta(days=14)
    if days <= 0:
        return None
    return timedelta(days=days)


#: Convenience for callers/tests that want the configured window.
IDLE_TIMEOUT = _idle_timeout()


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

    Returns ``None`` for missing / unknown / revoked / expired tokens, and for
    sessions idle longer than :data:`IDLE_TIMEOUT`. An over-idle session is
    also **revoked in the database**, so the row cannot be resurrected by a
    later request and "sign out everywhere" counts stay honest.

    Updates ``last_seen_at`` as a side effect so we can later show
    "last active 2 minutes ago" per session.
    """
    if not token:
        return None
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    async with get_connection() as conn:
        has_status = await status_column_available(conn)
        status_col = ", u.status" if has_status else ""
        cursor = await conn.execute(
            # noqa-worthy only in shape: ``status_col`` is one of two module
            # literals chosen by a boolean, never user input.
            "SELECT s.token, s.user_id, s.expires_at, s.revoked_at, "  # noqa: S608
            "       s.last_seen_at, s.issued_at, u.email, u.display_name"
            f"{status_col} "
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
        # Статус аккаунта проверяем НА КАЖДОМ запросе, а не только при выдаче:
        # тогда ``roles.set_status(uid, "suspended")`` вырубает доступ даже
        # если ревок сессий по какой-то причине не отработал (или сессию
        # выдали между двумя шагами). Строку тоже гасим — токен мёртв везде.
        if has_status and not is_active_status(status_of_row(row)):
            try:
                await conn.execute(
                    "UPDATE auth_session SET revoked_at = ? "
                    "WHERE token = ? AND revoked_at IS NULL",
                    (now, token),
                )
                await conn.commit()
            except Exception as exc:  # noqa: BLE001 — всё равно отказываем
                log.debug("auth.session.inactive_revoke_failed", error=str(exc))
            log.info("auth.session.refused_inactive", user_id=int(row["user_id"]))
            return None
        idle = _idle_timeout()
        if idle is not None and _is_idle(row, now_dt, idle):
            # Kill the row so this token is dead everywhere, not just here.
            try:
                await conn.execute(
                    "UPDATE auth_session SET revoked_at = ? "
                    "WHERE token = ? AND revoked_at IS NULL",
                    (now, token),
                )
                await conn.commit()
            except Exception as exc:  # noqa: BLE001 — still refuse the session
                log.debug("auth.session.idle_revoke_failed", error=str(exc))
            log.info("auth.session.idle_expired", user_id=int(row["user_id"]))
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


def _is_idle(row: object, now: datetime, idle: timedelta) -> bool:
    """True when the session's last activity is older than ``idle``.

    Falls back to ``issued_at`` when ``last_seen_at`` is NULL (rows created
    before that column was populated). An unparseable timestamp is treated as
    **not** idle: refusing every session because of a malformed date would lock
    the owner out of his own instance, and the absolute ``expires_at`` check
    above still bounds the damage.
    """
    stamp = None
    for column in ("last_seen_at", "issued_at"):
        try:
            value = row[column]  # type: ignore[index]
        except Exception:  # noqa: BLE001 — column absent on an old row shape
            value = None
        if value:
            stamp = str(value)
            break
    if not stamp:
        return False
    try:
        seen = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen) > idle


async def rotate_session(
    old_token: str | None,
    user_id: int,
    *,
    user_agent: str | None = None,
    lifetime: timedelta | None = None,
) -> tuple[str, datetime]:
    """Issue a fresh session for ``user_id`` and revoke ``old_token``.

    Call this on every privilege-relevant event — sign-in while a session
    cookie is already present, and password change. Keeping the same token
    across those is textbook session fixation: an attacker who plants a known
    token in a victim's browser (via a same-site injection, a shared machine,
    or a link that sets the cookie) rides it straight into the authenticated
    session afterwards.

    The new session is created **first**; the old one is revoked only after,
    so a failure mid-way leaves the user logged in rather than logged out of
    everything with no replacement cookie.
    """
    token, expires_at = await issue_session(
        user_id, user_agent=user_agent, lifetime=lifetime
    )
    if old_token and old_token != token:
        try:
            await revoke_session(old_token)
        except Exception as exc:  # noqa: BLE001 — new session already valid
            log.warning("auth.session.rotate_revoke_failed", error=str(exc))
    log.info("auth.session.rotated", user_id=int(user_id))
    return token, expires_at


async def revoke_all_for_user(user_id: int, *, keep_token: str | None = None) -> int:
    """Revoke every active session of ``user_id`` — "sign out everywhere".

    ``keep_token`` spares the caller's own session, which is what you want for
    "log out my other devices" after a password change: the person doing the
    change should not be kicked out of the tab they are looking at.
    Returns the number of sessions revoked.
    """
    now = datetime.now(timezone.utc).isoformat()
    params: list[object] = [now, int(user_id)]
    sql = (
        "UPDATE auth_session SET revoked_at = ? "
        "WHERE user_id = ? AND revoked_at IS NULL"
    )
    if keep_token:
        sql += " AND token <> ?"
        params.append(keep_token)
    async with get_connection() as conn:
        cursor = await conn.execute(sql, tuple(params))
        await conn.commit()
        changed = int(cursor.rowcount or 0)
    log.info(
        "auth.sessions.revoked_all",
        user_id=int(user_id),
        revoked=changed,
        kept_current=bool(keep_token),
    )
    return changed


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
