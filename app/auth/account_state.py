"""``users.status`` enforcement — suspension that actually suspends.

The hole this closes
--------------------
Migration 184 added ``users.role`` / ``users.status``, and
:func:`app.auth.roles.set_status` does the right thing on the write side: it
stamps the new status and revokes every live session of that account. But
**nothing on the read side ever looked at the column**. ``authenticate()``
checked the password hash and nothing else, and ``verify_session()`` joined
``users`` only to read the email and display name.

Net effect before this module: suspending an account logged the person out for
exactly as long as it took them to type their password again. "Suspended" was
a label in the admin list, not a control.

That matters tonight specifically: prod carries dormant non-owner accounts
(including one whose password nobody here knows) that become live the moment
``owner_exclusive_mode`` flips to ``0``. Suspension is the owner's kill-switch
for exactly that account, so it has to work.

What counts as "may sign in"
----------------------------
Only ``status = 'active'``. ``suspended`` is an explicit ban; ``pending`` means
"not admitted yet" — nothing in the codebase currently *writes* ``pending``, so
refusing it costs no existing flow and stops a future invite-gate from being
accidentally bypassable. An empty / NULL / unknown value is treated as active,
because ``NOT NULL DEFAULT 'active'`` means only a hand-edited row can produce
one and locking such a user out is the wrong failure.

Back-compatibility
------------------
An installation whose database predates migration 184 has no ``status``
column at all. Selecting it would raise on **every authentication**, which
would be a self-inflicted outage. :func:`status_column_available` probes
``PRAGMA table_info(users)`` once per process and caches the answer; when the
column is absent every account is treated as active — exactly the old
behaviour.

Fail direction
--------------
Enforcement failing *open* (letting a suspended user in) is the bad outcome
here, so the probe defaults to "column exists, enforce" and only a definitive
"the column is not there" answer disables the check. A transient DB error
during the probe leaves the cache unset so the next call retries rather than
memoising a wrong answer.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger

log = get_logger("persona.auth.account_state")

__all__ = [
    "ACTIVE",
    "AccountInactiveError",
    "is_active_status",
    "reset_probe",
    "status_column_available",
    "status_of_row",
]

ACTIVE = "active"

# None = not probed yet. Deliberately not a plain bool default: a failed probe
# must retry, not freeze an unverified answer.
_has_status_column: bool | None = None


class AccountInactiveError(Exception):
    """Credentials were correct, but the account may not sign in.

    Raised — rather than returned as ``None`` — on purpose. There is exactly
    one caller today, and an exception means a *future* caller that forgets to
    handle suspension gets a loud 500 instead of silently admitting a banned
    account. Fail closed, noisily.
    """

    def __init__(self, status: str) -> None:
        super().__init__(f"account is not active: {status}")
        self.status = status


def is_active_status(status: object) -> bool:
    """True when ``status`` permits sign-in.

    NULL / empty / unknown → active (see the module docstring): the column is
    ``NOT NULL DEFAULT 'active'``, so anything else is a hand-edit, and the
    safe reading of a hand-edit is "this row predates the feature".
    """
    if status is None:
        return True
    text = str(status).strip().lower()
    if not text:
        return True
    return text == ACTIVE


def status_of_row(row: Any) -> str | None:
    """Pull ``status`` out of a sqlite Row without exploding when absent."""
    try:
        return row["status"]  # type: ignore[index]
    except Exception:  # noqa: BLE001 — column not selected / not present
        return None


async def status_column_available(conn: Any) -> bool:
    """Whether ``users.status`` exists. Probed once per process, then cached."""
    global _has_status_column  # noqa: PLW0603 — one-bit process memo
    if _has_status_column is not None:
        return _has_status_column
    try:
        cursor = await conn.execute("PRAGMA table_info(users)")
        rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — retry next time, do not memoise
        log.debug("auth.status_probe_failed", error=str(exc))
        return True
    present = any(str(row[1]) == "status" for row in rows)
    _has_status_column = present
    if not present:
        log.warning(
            "auth.status_column_missing",
            hint=(
                "users.status is absent (database predates migration 184) — "
                "account suspension cannot be enforced on this install."
            ),
        )
    return present


def reset_probe() -> None:
    """Forget the cached probe result (tests)."""
    global _has_status_column  # noqa: PLW0603
    _has_status_column = None
