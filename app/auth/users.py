"""User row CRUD: create, lookup by email, update password / display name."""

from __future__ import annotations

import re
from typing import TypedDict

from app.auth.account_state import (
    AccountInactiveError,
    is_active_status,
    status_column_available,
    status_of_row,
)
from app.auth.password_policy import MIN_LENGTH as _MIN_PASSWORD_LEN_POLICY
from app.auth.password_policy import check_password
from app.auth.passwords import hash_password, verify_password
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.auth.users")

# Liberal email validation — accept anything that has the shape ``a@b.c``.
# We never *deliver* email, so the only goal is to keep junk out of the
# UNIQUE column. Stricter regexes reject many valid addresses (RFC 5321 is
# legendarily permissive); this rule covers the practical cases.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_MIN_PASSWORD_LEN = _MIN_PASSWORD_LEN_POLICY  # 8 — unchanged, now enforced centrally
_MAX_EMAIL_LEN = 254  # RFC 5321 cap


class UserRow(TypedDict):
    id: int
    email: str
    display_name: str | None


def normalise_email(email: str) -> str:
    """Trim and lowercase ``email``. Raises ValueError on shape errors."""
    cleaned = email.strip().lower()
    if not cleaned or len(cleaned) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(cleaned):
        raise ValueError("invalid email")
    return cleaned


def validate_password(password: str, *, email: str | None = None) -> None:
    """Raise ``ValueError`` when ``password`` does not meet the floor.

    Delegates to :mod:`app.auth.password_policy`: length floor/ceiling, an
    embedded worst-passwords blocklist (with trailing-decoration stripping, so
    ``qwerty123`` is caught too), keyboard/alphabet runs, and — when ``email``
    is supplied — "password contains your own address".

    The error strings are the same stable English keys as before, so the RU
    translation table in the auth routes keeps matching.
    """
    check_password(password, email=email)


async def create_user(
    email: str, password: str, display_name: str | None = None
) -> UserRow:
    """Insert a new user. Raises ValueError on duplicate email or bad input."""
    norm_email = normalise_email(email)
    validate_password(password, email=norm_email)
    hashed = hash_password(password)
    clean_display = (display_name or "").strip() or None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM users WHERE email = ? LIMIT 1", (norm_email,)
        )
        if await cursor.fetchone() is not None:
            raise ValueError("email already registered")
        cursor = await conn.execute(
            "INSERT INTO users (email, password_hash, display_name) "
            "VALUES (?, ?, ?)",
            (norm_email, hashed, clean_display),
        )
        await conn.commit()
        user_id = cursor.lastrowid or 0
    log.info("auth.user.created", user_id=user_id)
    return {"id": user_id, "email": norm_email, "display_name": clean_display}


async def authenticate(email: str, password: str) -> UserRow | None:
    """Return the user row when credentials match, else ``None``.

    Raises :class:`AccountInactiveError` when the password is **correct** but
    ``users.status`` is not ``active``. The order matters and is deliberate:

    * a wrong password always yields the same ``None`` no matter what the
      account's status is, so nobody can probe "is this address suspended?"
      without already knowing the password — no enumeration;
    * a right password on a banned account raises instead of returning a row,
      so the failure is impossible to ignore at the call site.

    Before this check, :func:`app.auth.roles.set_status` was cosmetic: it
    revoked the sessions of a suspended account, and the account simply logged
    back in.
    """
    try:
        norm_email = normalise_email(email)
    except ValueError:
        return None
    async with get_connection() as conn:
        has_status = await status_column_available(conn)
        columns = "id, email, password_hash, display_name" + (
            ", status" if has_status else ""
        )
        cursor = await conn.execute(
            f"SELECT {columns} FROM users WHERE email = ?",  # noqa: S608 — literal
            (norm_email,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        stored_hash = str(row["password_hash"])
        if not verify_password(password, stored_hash):
            return None
        if has_status:
            status = status_of_row(row)
            if not is_active_status(status):
                log.warning(
                    "auth.user.refused_inactive",
                    user_id=int(row["id"]),
                    status=str(status),
                )
                raise AccountInactiveError(str(status))
        # Successful login bumps last_login_at; fire-and-forget so a
        # failure in the audit-trail update never blocks the user.
        try:
            await conn.execute(
                "UPDATE users SET last_login_at = datetime('now') WHERE id = ?",
                (int(row["id"]),),
            )
            await conn.commit()
        except Exception as exc:
            log.debug("auth.user.last_login_update_failed", error=str(exc))
    return {
        "id": int(row["id"]),
        "email": str(row["email"]),
        "display_name": (
            str(row["display_name"]) if row["display_name"] is not None else None
        ),
    }


async def update_password(user_id: int, new_password: str) -> None:
    """Set a new password (no old-password check — used after email reset).

    The account's own email is fetched first so the strength check can reject
    ``ivan@…`` / ``ivan12345``. A lookup failure is not fatal — the rest of the
    policy still applies — because refusing to let someone set a password just
    because a SELECT hiccuped is a worse outcome than a slightly weaker check.

    The **caller** is responsible for rotating the session afterwards
    (:func:`app.auth.sessions.rotate_session`) and for revoking the account's
    other sessions; doing it here would need the current token, which this
    layer does not see.
    """
    owner_email: str | None = None
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT email FROM users WHERE id = ?", (int(user_id),)
            )
            row = await cursor.fetchone()
            if row is not None:
                owner_email = str(row["email"])
    except Exception as exc:  # noqa: BLE001 — policy still applies without it
        log.debug("auth.user.email_lookup_failed", error=str(exc))
    validate_password(new_password, email=owner_email)
    hashed = hash_password(new_password)
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id)
        )
        await conn.commit()
    log.info("auth.user.password_changed", user_id=user_id)


async def is_account_active(user_id: int | None) -> bool:
    """True when ``user_id`` exists and may sign in.

    Used by the passwordless flows (magic link, password reset), which mint a
    session **without** ever calling :func:`authenticate` and would otherwise be
    a way around suspension entirely.

    Fail direction: a lookup error returns ``False``. Refusing a magic link
    during a transient DB blip is recoverable (retry); admitting a suspended
    account because a SELECT hiccuped is not.
    """
    if user_id is None:
        return False
    try:
        async with get_connection() as conn:
            if not await status_column_available(conn):
                # Pre-184 database: no column, nothing to enforce — but the
                # account must still exist.
                cursor = await conn.execute(
                    "SELECT 1 FROM users WHERE id = ?", (int(user_id),)
                )
                return await cursor.fetchone() is not None
            cursor = await conn.execute(
                "SELECT status FROM users WHERE id = ?", (int(user_id),)
            )
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 — deny on error, never admit
        log.warning("auth.user.status_lookup_failed", error=str(exc))
        return False
    if row is None:
        return False
    return is_active_status(status_of_row(row))


async def count_users() -> int:
    """Return the total number of registered users. Used by the landing
    gate so a brand-new install still falls through to /setup instead of
    bouncing on /auth/login forever."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM users")
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0
