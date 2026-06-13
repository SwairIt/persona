"""Waitlist — collect emails for early access.

Because the app is not yet isolated per-user, open self-serve registration
is intentionally gated. When someone enters an email that has no account,
we add them to the waitlist instead of creating an account (which would
expose the owner's data). The owner can later invite them.
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.waitlist")


async def add_to_waitlist(email: str, source: str = "landing") -> bool:
    """Insert an email (idempotent via UNIQUE). Returns False on error."""
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO waitlist (email, source) VALUES (?, ?)",
                (email, source),
            )
            await conn.commit()
        log.info("waitlist.add", email=email, source=source)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("waitlist.add_failed", error=str(exc))
        return False
