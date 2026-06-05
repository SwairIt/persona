"""Per-app usage budget caps (v1.45).

The operator picks an app (matched against ``screenshots.app_name``
verbatim) and assigns a daily-minutes cap. The background worker
(``app/workers/app_budget_worker.py``) periodically tallies today's
captured minutes per enabled budget and fires a notification — once
per app per day — when the cap is breached.

"Captured minutes" is computed as a proxy: ``shots_today * interval / 60``
where ``interval`` is the live ``capture_interval_seconds`` (kv, falling
back to the settings default). It is close enough for the alerting use
case; the operator's mental model is "Twitter ate 30+ minutes of my
day", not "Twitter was the foreground window for exactly 31m 12s".

The kv row ``app_budget_check_enabled`` ("1"/"0", default "1") lets the
operator pause the entire alerting subsystem from the same page — the
worker checks it each cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.app_budgets")

_ALLOWED_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "warn"})
"""Mirror of the SQL CHECK constraint on ``app_budget.alert_severity``.

Pre-validating at the helper level keeps the error surface a tidy
:class:`ValueError` at the form-handler call site rather than a deep
:class:`aiosqlite.IntegrityError`."""


async def _read_interval_seconds() -> float:
    """Return the live ``capture_interval_seconds``.

    Prefers the kv override (the same key ``rate_advisor`` writes to)
    and falls back to the settings default. Any non-numeric kv value is
    treated as "no override" and logged so a corrupt row doesn't make
    the budget tally silently wrong.
    """
    cfg = get_settings()
    async with get_connection() as conn:
        raw = await get_kv(conn, "capture_interval_seconds")
    if raw is None:
        return float(cfg.capture_interval_seconds)
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("app_budgets.interval_kv_invalid", raw=raw)
        return float(cfg.capture_interval_seconds)


async def list_budgets() -> list[dict[str, Any]]:
    """Return every configured budget, ordered by app_name."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, app_name, daily_minutes_cap, enabled, alert_severity, created_at "
            "FROM app_budget ORDER BY app_name"
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "app_name": str(row["app_name"]),
            "daily_minutes_cap": int(row["daily_minutes_cap"]),
            "enabled": bool(row["enabled"]),
            "alert_severity": str(row["alert_severity"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def upsert_budget(
    app_name: str,
    daily_minutes_cap: int,
    enabled: bool = True,
    alert_severity: str = "info",
) -> int:
    """Insert or update one budget row, return the row id.

    Raises :class:`ValueError` for blank app names, non-positive caps,
    or severities outside :data:`_ALLOWED_SEVERITIES` — the form handler
    surfaces these as a 400.
    """
    name = app_name.strip()
    if not name:
        msg = "app_name is required"
        raise ValueError(msg)
    if daily_minutes_cap < 0:
        msg = "daily_minutes_cap must be >= 0"
        raise ValueError(msg)
    if alert_severity not in _ALLOWED_SEVERITIES:
        msg = (
            f"alert_severity must be one of {sorted(_ALLOWED_SEVERITIES)!r}, "
            f"got {alert_severity!r}"
        )
        raise ValueError(msg)

    enabled_int = 1 if enabled else 0
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_budget
                (app_name, daily_minutes_cap, enabled, alert_severity)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(app_name) DO UPDATE SET
                daily_minutes_cap = excluded.daily_minutes_cap,
                enabled = excluded.enabled,
                alert_severity = excluded.alert_severity
            """,
            (name, int(daily_minutes_cap), enabled_int, alert_severity),
        )
        cursor = await conn.execute(
            "SELECT id FROM app_budget WHERE app_name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        await conn.commit()

    if row is None:
        # The INSERT … ON CONFLICT path is unconditional, so a missing
        # row after the SELECT means a foreign hand deleted the row
        # between statements. Surface loudly — callers expect an id.
        msg = "app_budget row vanished after upsert"
        raise RuntimeError(msg)

    budget_id = int(row["id"])
    log.info(
        "app_budgets.upsert",
        budget_id=budget_id,
        app_name=name,
        cap=int(daily_minutes_cap),
        enabled=enabled_int,
        severity=alert_severity,
    )
    return budget_id


async def delete_budget(budget_id: int) -> None:
    """Remove one budget row. Missing ids are a no-op (idempotent)."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM app_budget WHERE id = ?",
            (int(budget_id),),
        )
        await conn.commit()
    log.info("app_budgets.delete", budget_id=int(budget_id))


async def toggle_budget(budget_id: int) -> bool:
    """Flip ``enabled`` for ``budget_id`` and return the new state.

    Returns ``False`` when the id does not exist — the caller's
    redirect-back UI shrugs that case off.
    """
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE app_budget SET enabled = 1 - enabled WHERE id = ?",
            (int(budget_id),),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT enabled FROM app_budget WHERE id = ?",
            (int(budget_id),),
        )
        row = await cursor.fetchone()
    new_state = bool(row["enabled"]) if row is not None else False
    log.info("app_budgets.toggle", budget_id=int(budget_id), enabled=new_state)
    return new_state


async def check_today_status() -> list[dict[str, Any]]:
    """Return today's per-budget tally.

    One entry per *enabled* budget row, shape::

        {
            "id": int,
            "app_name": str,
            "used_minutes": float,
            "cap_minutes": int,
            "percent": float,    # used / cap * 100, clamped to >= 0
            "alert_severity": str,
            "breached_at": str | None,  # ISO-now when used >= cap, else None
        }

    ``used_minutes`` is the shot-count proxy described in the module
    docstring. We deliberately do *not* round it: the UI rounds for
    display, the worker compares the raw float so a sub-minute spill
    still counts.
    """
    interval = await _read_interval_seconds()
    minutes_per_shot = interval / 60.0
    now_iso = datetime.now(tz=UTC).isoformat()
    today = datetime.now(tz=UTC).date().isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, app_name, daily_minutes_cap, alert_severity "
            "FROM app_budget WHERE enabled = 1 ORDER BY app_name"
        )
        budgets = await cursor.fetchall()

        out: list[dict[str, Any]] = []
        for b in budgets:
            app_name = str(b["app_name"])
            cap = int(b["daily_minutes_cap"])
            # ``date(captured_at)`` indexes the day boundary using the
            # captured_at TEXT format (ISO). SQLite's ``date()`` accepts
            # the same string format the capture loop writes.
            shot_cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM screenshots "
                "WHERE app_name = ? AND date(captured_at) = ?",
                (app_name, today),
            )
            shot_row = await shot_cursor.fetchone()
            shots = int(shot_row["n"]) if shot_row is not None else 0
            used = shots * minutes_per_shot
            percent = (used / cap * 100.0) if cap > 0 else 100.0
            breached_at = now_iso if used >= cap else None
            out.append(
                {
                    "id": int(b["id"]),
                    "app_name": app_name,
                    "used_minutes": used,
                    "cap_minutes": cap,
                    "percent": max(0.0, percent),
                    "alert_severity": str(b["alert_severity"]),
                    "breached_at": breached_at,
                }
            )

    log.info("app_budgets.status", entries=len(out))
    return out


__all__ = [
    "check_today_status",
    "delete_budget",
    "list_budgets",
    "toggle_budget",
    "upsert_budget",
]
