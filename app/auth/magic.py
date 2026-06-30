"""Passwordless magic-link login.

Flow:
  1. ``create_magic_link(email)`` mints a single-use token (30 min TTL),
     stored in the ``magic_link`` table. The caller emails a URL containing
     it (``/auth/magic/<token>``).
  2. ``consume_magic_link(token)`` validates it (exists, not used, not
     expired), marks it used, and returns the email so the route can issue
     a session.

Security:
  * Tokens are 256-bit url-safe random — unguessable.
  * Single-use: ``used_at`` is stamped on first consume.
  * Short TTL limits the window if a link leaks.
  * We only ever MINT a link for an email that already has an account
    (the route checks). Unknown emails go to the waitlist, never get a
    link — so a magic link can never create or take over an account.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from app.storage.db import get_connection

_TTL_MINUTES = 30
_TS_FMT = "%Y-%m-%d %H:%M:%S"


async def create_magic_link(email: str) -> str:
    """Mint a single-use login token for ``email`` and return it."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(UTC) + timedelta(minutes=_TTL_MINUTES)).strftime(_TS_FMT)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO magic_link (token, email, expires_at) VALUES (?, ?, ?)",
            (token, email, expires),
        )
        await conn.commit()
    return token


async def consume_magic_link(token: str) -> str | None:
    """Validate + burn a token. Returns the email, or None if invalid.

    Потребление атомарно: один ``UPDATE ... WHERE token=? AND used_at IS NULL``
    помечает ссылку использованной. Если ``rowcount != 1`` — кто-то уже сжёг
    ссылку (двойной клик / гонка) или токена нет → возвращаем None. Так
    одноразовость гарантируется на уровне БД, без TOCTOU между SELECT и UPDATE.
    Срок (30 мин) проверяем отдельным SELECT, чтобы не «жечь» просроченную.
    """
    if not token:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT email, expires_at, used_at FROM magic_link WHERE token = ?",
            (token,),
        )
        row = await cursor.fetchone()
        if row is None or row["used_at"]:
            return None
        try:
            expires = datetime.strptime(row["expires_at"], _TS_FMT).replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None
        if datetime.now(UTC) > expires:
            return None
        # Атомарный «burn»: пометим использованной только если ещё не была.
        upd = await conn.execute(
            "UPDATE magic_link SET used_at = datetime('now') "
            "WHERE token = ? AND used_at IS NULL",
            (token,),
        )
        await conn.commit()
        if upd.rowcount != 1:
            # Проиграли гонку — ссылку уже сожгли в параллельном запросе.
            return None
        return str(row["email"])
