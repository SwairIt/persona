"""User row CRUD: create, lookup by email, update password / display name."""

from __future__ import annotations

import re
from typing import TypedDict

from app.auth.passwords import hash_password, verify_password
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.auth.users")

# Liberal email validation — accept anything that has the shape ``a@b.c``.
# We never *deliver* email, so the only goal is to keep junk out of the
# UNIQUE column. Stricter regexes reject many valid addresses (RFC 5321 is
# legendarily permissive); this rule covers the practical cases.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_MIN_PASSWORD_LEN = 8
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


def validate_password(password: str) -> None:
    """Raise ``ValueError`` when ``password`` does not meet the floor."""
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {_MIN_PASSWORD_LEN} characters")


async def create_user(
    email: str, password: str, display_name: str | None = None
) -> UserRow:
    """Insert a new user. Raises ValueError on duplicate email or bad input."""
    norm_email = normalise_email(email)
    validate_password(password)
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
    """Return the user row when credentials match, else ``None``."""
    try:
        norm_email = normalise_email(email)
    except ValueError:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, email, password_hash, display_name "
            "FROM users WHERE email = ?",
            (norm_email,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        stored_hash = str(row["password_hash"])
        if not verify_password(password, stored_hash):
            return None
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
    """Set a new password (no old-password check — used after email reset)."""
    validate_password(new_password)
    hashed = hash_password(new_password)
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id)
        )
        await conn.commit()
    log.info("auth.user.password_changed", user_id=user_id)


async def count_users() -> int:
    """Return the total number of registered users. Used by the landing
    gate so a brand-new install still falls through to /setup instead of
    bouncing on /auth/login forever."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM users")
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0
