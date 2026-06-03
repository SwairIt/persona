"""Settings diff viewer — recent ``kv_settings`` changes from the audit log.

Reads the append-only ``audit_log`` table (see :mod:`app.audit`) for
entries whose ``action`` matches ``settings.%`` within the last ``days``
window and pairs them chronologically per key so the operator sees
``(old_value -> new_value)`` for each change.

The diff is *derived*: the audit log stores each settings change as a
single row whose ``detail`` field holds the value that was written. We
sort rows per key ascending by id and treat each row's ``detail`` as the
``new_value`` while the *previous* row's ``detail`` for that key (or
``None`` for the first observed change) is the ``old_value``. This keeps
the page useful without requiring a schema change to ``audit_log``.

* ``GET /admin/settings-diff?days=30``  renders the HTML page.
* ``GET /api/settings-diff.json``       returns the same payload as JSON.

Both endpoints are read-only and never mutate ``audit_log`` or
``kv_settings`` — the route is for inspection during incident review.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import aiosqlite
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Sequence

router = APIRouter(tags=["settings-diff"])
log = get_logger("persona.settings_diff")

# Inclusive lower / upper bounds on the ``?days=`` window. ``1`` keeps the
# query meaningful (anything shorter than a day collapses to "no rows"
# given SQLite's second-resolution clock) and ``365`` caps the worst
# case at one year so an accidental ``?days=10**9`` doesn't ask SQLite
# to scan the audit log unbounded.
_DAYS_MIN = 1
_DAYS_MAX = 365
_DAYS_DEFAULT = 30

# Hard ceiling on rows returned in a single response. The audit log is
# small in practice (privileged actions only) but capping here keeps the
# JSON payload predictable for tooling.
_LIST_HARD_CAP = 2000

# All ``settings.*`` actions live under this prefix. The ``%`` lives in
# the parameter binding, never in interpolated SQL.
_SETTINGS_ACTION_PREFIX = "settings."


class SettingsDiffRow(TypedDict):
    """One change to a ``kv_settings`` key, derived from two audit rows."""

    key: str
    old_value: str | None
    new_value: str | None
    ts: str
    actor: str | None
    action: str


def _clamp_days(days: int) -> int:
    """Bound the ``days`` query parameter to ``[_DAYS_MIN, _DAYS_MAX]``.

    A value below the minimum collapses to the minimum so the page still
    renders something useful instead of an empty table; values above the
    ceiling are clamped so SQLite never sees a wildly-large interval.
    """
    if days < _DAYS_MIN:
        return _DAYS_MIN
    if days > _DAYS_MAX:
        return _DAYS_MAX
    return days


async def _fetch_settings_changes(days: int) -> list[SettingsDiffRow]:
    """Load and pair ``settings.*`` audit rows from the last ``days`` days.

    SQL is fully static; the ``LIKE`` prefix and the ``-N days`` modifier
    travel as ``?`` placeholders so no user input is ever interpolated
    into the query string.
    """
    safe_days = _clamp_days(int(days))
    # ``datetime('now', ?)`` accepts a modifier like ``"-30 days"`` —
    # SQLite parses it the same as a literal modifier, with the integer
    # cast happening on our side before formatting.
    modifier = f"-{safe_days} days"

    sql = (
        "SELECT id, ts, action, actor, target, detail "
        "FROM audit_log "
        "WHERE action LIKE ? "
        "  AND ts >= datetime('now', ?) "
        "  AND target IS NOT NULL "
        "ORDER BY target ASC, id ASC "
        "LIMIT ?"
    )
    params: Sequence[object] = (
        f"{_SETTINGS_ACTION_PREFIX}%",
        modifier,
        _LIST_HARD_CAP,
    )

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        log.warning("settings_diff.fetch.failed", days=safe_days, error=str(exc))
        return []

    # Walk per-key in chronological order; for each row, the previous
    # detail for that key (or ``None``) becomes the ``old_value`` and the
    # current detail becomes the ``new_value``. The SQL ``ORDER BY target,
    # id`` guarantees the grouping without an extra Python sort.
    diffs: list[SettingsDiffRow] = []
    previous_value_per_key: dict[str, str | None] = {}

    for row in rows:
        key = str(row["target"])
        new_value = None if row["detail"] is None else str(row["detail"])
        old_value = previous_value_per_key.get(key)
        diffs.append(
            SettingsDiffRow(
                key=key,
                old_value=old_value,
                new_value=new_value,
                ts=str(row["ts"]),
                actor=(None if row["actor"] is None else str(row["actor"])),
                action=str(row["action"]),
            )
        )
        previous_value_per_key[key] = new_value

    # Present newest changes first — operators care about "what just
    # changed" more than the full historical replay.
    diffs.sort(key=lambda entry: entry["ts"], reverse=True)
    return diffs


@router.get("/admin/settings-diff", response_class=HTMLResponse)
async def settings_diff_page(
    request: Request,
    days: int = Query(default=_DAYS_DEFAULT),
) -> HTMLResponse:
    """Render the kv_settings diff table for the last ``days`` days."""
    safe_days = _clamp_days(int(days))
    rows = await _fetch_settings_changes(safe_days)
    log.info(
        "settings_diff.page",
        days=safe_days,
        rows=len(rows),
    )
    return templates.TemplateResponse(
        request,
        "settings_diff.html",
        {
            "title": "Settings diff",
            "active_nav": "settings",
            "rows": rows,
            "days": safe_days,
            "days_min": _DAYS_MIN,
            "days_max": _DAYS_MAX,
            "total_rows": len(rows),
        },
    )


@router.get("/api/settings-diff.json", response_class=JSONResponse)
async def settings_diff_json(
    days: int = Query(default=_DAYS_DEFAULT),
) -> JSONResponse:
    """JSON projection of the same diff payload for tooling / dashboards."""
    safe_days = _clamp_days(int(days))
    rows = await _fetch_settings_changes(safe_days)
    log.info(
        "settings_diff.api",
        days=safe_days,
        rows=len(rows),
    )
    return JSONResponse(
        {
            "rows": list(rows),
            "days": safe_days,
            "total": len(rows),
        }
    )


__all__ = ["router"]
