"""Per-app retention overrides.

Global retention treats every screenshot the same — same warm cutoff,
same cold cutoff, same eventual purge window. In practice some apps
deserve different policies: source-of-truth windows (VS Code, design
tools) often want to live forever, while chatter (Slack, IM clients)
can demote and disappear faster than the global defaults.

This module owns the data plane for the ``app_retention`` table
introduced by migration ``048_app_retention.sql``. Each row is a
*sparse* override for a single ``app_name``: any of the three numeric
columns may be ``NULL`` to mean "inherit from
:class:`app.settings.Settings`". The :data:`never_delete` flag is a
hard switch — when set, the retention worker skips the row entirely
and the screenshot keeps its hot thumbnail indefinitely.

The HTTP layer lives in :mod:`app.web.routes.app_retention`; the
worker integration is a single lookup in
:mod:`app.workers.retention`.
"""

from __future__ import annotations

from typing import Any, Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.retention.per_app")

# Same range as ``Settings.tier_warm_after_days``/``tier_cold_after_days``
# /``retention_days``. We re-state it here so the helper can reject
# garbage input before it hits SQLite (SQLite has no CHECK on INTEGER
# range without a custom constraint). Anything outside this window is
# either a typo or pathological.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 3650


class AppRetention(TypedDict):
    """Shape returned by every public helper.

    Each numeric field is ``None`` when the operator wants to inherit
    that knob from the global settings; ``never_delete`` is the hard
    "skip this app entirely" switch the retention worker checks first.
    """

    app_name: str
    warm_after_days: int | None
    cold_after_days: int | None
    delete_after_days: int | None
    never_delete: bool


def _row_to_dict(row: Any) -> AppRetention:
    return AppRetention(
        app_name=str(row["app_name"]),
        warm_after_days=(
            int(row["warm_after_days"])
            if row["warm_after_days"] is not None
            else None
        ),
        cold_after_days=(
            int(row["cold_after_days"])
            if row["cold_after_days"] is not None
            else None
        ),
        delete_after_days=(
            int(row["delete_after_days"])
            if row["delete_after_days"] is not None
            else None
        ),
        never_delete=bool(int(row["never_delete"])),
    )


def _clean_app_name(app_name: str) -> str:
    cleaned = (app_name or "").strip()
    if not cleaned:
        msg = "app_name is required"
        raise ValueError(msg)
    return cleaned


def _validate_days(label: str, value: int | None) -> int | None:
    """Return ``value`` if it falls inside the global range, else raise.

    ``None`` (inherit from settings) is always accepted as-is.
    """
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{label} must be an int, got {type(value).__name__}"
        raise ValueError(msg)
    if not (_MIN_DAYS <= value <= _MAX_DAYS):
        msg = f"{label} must be between {_MIN_DAYS} and {_MAX_DAYS}, got {value}"
        raise ValueError(msg)
    return value


async def set_override(
    app_name: str,
    warm: int | None,
    cold: int | None,
    delete: int | None,
    never: bool,
) -> None:
    """Insert or update the override row for ``app_name``.

    ``warm``/``cold``/``delete`` are each accepted as ``None`` to mean
    "inherit the global setting". ``never`` is the hard skip switch.
    The order of the columns in the table is intentionally aligned with
    the worker's check sequence (warm → cold → delete) so the helper's
    signature reads top-to-bottom like the policy it encodes.
    """
    name = _clean_app_name(app_name)
    warm_clean = _validate_days("warm_after_days", warm)
    cold_clean = _validate_days("cold_after_days", cold)
    delete_clean = _validate_days("delete_after_days", delete)
    if (
        warm_clean is not None
        and cold_clean is not None
        and warm_clean > cold_clean
    ):
        msg = (
            "warm_after_days must be <= cold_after_days when both are set "
            f"(got warm={warm_clean}, cold={cold_clean})"
        )
        raise ValueError(msg)
    if (
        cold_clean is not None
        and delete_clean is not None
        and cold_clean > delete_clean
    ):
        msg = (
            "cold_after_days must be <= delete_after_days when both are set "
            f"(got cold={cold_clean}, delete={delete_clean})"
        )
        raise ValueError(msg)
    never_int = 1 if never else 0

    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_retention (
                app_name, warm_after_days, cold_after_days,
                delete_after_days, never_delete, updated_at
            )
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(app_name) DO UPDATE SET
                warm_after_days = excluded.warm_after_days,
                cold_after_days = excluded.cold_after_days,
                delete_after_days = excluded.delete_after_days,
                never_delete = excluded.never_delete,
                updated_at = datetime('now')
            """,
            (name, warm_clean, cold_clean, delete_clean, never_int),
        )
        await conn.commit()
    log.info(
        "retention.per_app.set",
        app_name=name,
        warm_after_days=warm_clean,
        cold_after_days=cold_clean,
        delete_after_days=delete_clean,
        never_delete=bool(never_int),
    )


async def get_override(app_name: str) -> AppRetention | None:
    """Return the override row for ``app_name`` or ``None`` if absent."""
    name = _clean_app_name(app_name)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, warm_after_days, cold_after_days, "
            "delete_after_days, never_delete "
            "FROM app_retention WHERE app_name = ?",
            (name,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def list_overrides() -> list[AppRetention]:
    """Return every override row, alphabetised by ``app_name``."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, warm_after_days, cold_after_days, "
            "delete_after_days, never_delete "
            "FROM app_retention ORDER BY app_name"
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def remove_override(app_name: str) -> None:
    """Delete the override row for ``app_name``. No-op if absent."""
    name = _clean_app_name(app_name)
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM app_retention WHERE app_name = ?",
            (name,),
        )
        await conn.commit()
    log.info("retention.per_app.remove", app_name=name)


__all__ = [
    "AppRetention",
    "get_override",
    "list_overrides",
    "remove_override",
    "set_override",
]
