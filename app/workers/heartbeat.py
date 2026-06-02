"""Worker heartbeat tracking.

Every background worker is expected to call :func:`beat` at the top of
each loop iteration. The call upserts a row into ``worker_heartbeat``
keyed by worker name, refreshes ``last_run_at`` to the current UTC
timestamp, sets ``last_status`` to the caller-supplied label, and
increments ``ticks``.

The ``/admin/health`` dashboard reads :func:`get_all` to render uptime
plus a colour-coded freshness indicator (see
``app.web.routes.health_dashboard``).

Failure policy: heartbeat writes are best-effort. If the DB write
raises we log a warning but **never** propagate — a transient SQLite
hiccup must not crash the worker loop itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso, parse_iso

log = get_logger("persona.heartbeat")


class HeartbeatRow(TypedDict):
    """Snapshot of one worker's heartbeat for the dashboard / JSON API."""

    name: str
    last_run_at: str
    last_status: str | None
    ticks: int
    seconds_since: float


async def beat(name: str, status: str = "ok") -> None:
    """Record a heartbeat for ``name``.

    Upserts the row, refreshes ``last_run_at`` to UTC now, sets
    ``last_status`` to ``status``, and bumps ``ticks`` by one. All DB
    errors are swallowed and logged — workers must never crash because
    of a missed heartbeat.
    """
    now_iso = iso(datetime.now(timezone.utc))
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO worker_heartbeat (name, last_run_at, last_status, ticks) "
                "VALUES (?, ?, ?, 1) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  last_run_at = excluded.last_run_at, "
                "  last_status = excluded.last_status, "
                "  ticks = worker_heartbeat.ticks + 1",
                (name, now_iso, status),
            )
            await conn.commit()
    except Exception as exc:
        log.warning("heartbeat.write_failed", name=name, error=str(exc))


async def get_all() -> list[HeartbeatRow]:
    """Return every recorded worker heartbeat, sorted by name.

    ``seconds_since`` is computed at read time against the current UTC
    clock so the caller always sees an up-to-date freshness value
    without having to parse ``last_run_at`` itself.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name, last_run_at, last_status, ticks "
                "FROM worker_heartbeat ORDER BY name ASC"
            )
            rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("heartbeat.read_failed", error=str(exc))
        return []

    now = datetime.now(timezone.utc)
    out: list[HeartbeatRow] = []
    for row in rows:
        last_run_raw = str(row["last_run_at"])
        try:
            last_run = parse_iso(last_run_raw)
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            seconds_since = max(0.0, (now - last_run).total_seconds())
        except (ValueError, TypeError):
            seconds_since = -1.0

        last_status_raw = row["last_status"]
        last_status: str | None = (
            str(last_status_raw) if last_status_raw is not None else None
        )

        out.append(
            HeartbeatRow(
                name=str(row["name"]),
                last_run_at=last_run_raw,
                last_status=last_status,
                ticks=int(row["ticks"]),
                seconds_since=round(seconds_since, 3),
            )
        )
    return out


__all__ = ["HeartbeatRow", "beat", "get_all"]
