"""App-group categorisation — bucket ``Code.exe`` under ``dev``, ``slack.exe`` under ``comms``.

The capture loop persists the raw Win32 executable / window-class string
on every screenshot row (``app_name``). That column is great for exact
identification but the user often wants a coarser lens: "how much time
did I spend on **work** apps this month?" rather than "how much on
``devenv.exe`` plus ``Code.exe`` plus ``OUTLOOK.EXE``?".

This module owns the overlay that answers that question:

* **CRUD helpers** — :func:`set_group` / :func:`get_group` /
  :func:`list_all` / :func:`delete_group` — admin UI and tests use
  these. They go through :func:`app.storage.db.get_connection` so they
  share the same WAL / FK pragma setup as the rest of the app.

* **Aggregate** — :func:`totals_by_group` walks the last ``days`` days
  of ``screenshots`` rows, joins against ``app_group`` and folds
  per-app numbers into per-group totals. The seconds figure uses the
  same gap-capped definition as :mod:`app.time_on_app` so the two
  dashboards never disagree.

Naming convention: there is no enum of allowed group names. The UI
hints at ``work / personal / comms / dev / games`` but anything the
user types is accepted — the table is a free-form mapping, not a
controlled vocabulary. The absence of a row means "no group" and the
aggregate skips those apps entirely (rather than lumping them into a
phantom ``"ungrouped"`` bucket the user never asked for).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.time_on_app import DEFAULT_MAX_GAP_SECONDS, AppTime, _walk_day_rows

log = get_logger("persona.app_groups")


class GroupTotal(TypedDict):
    """One row of :func:`totals_by_group` output."""

    group_name: str
    shots: int
    total_seconds: int


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


async def set_group(app_name: str, group_name: str) -> None:
    """Upsert ``group_name`` as the bucket for ``app_name``.

    Both inputs are stripped; ``app_name`` is required (raises
    :class:`ValueError`). An empty ``group_name`` is treated as "remove
    from any group" and routed through :func:`delete_group` so the row
    never lingers as a no-op overlay.
    """
    app_key = app_name.strip()
    group_key = group_name.strip()
    if not app_key:
        msg = "app_name is required"
        raise ValueError(msg)
    if not group_key:
        await delete_group(app_key)
        return
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_group (app_name, group_name)
            VALUES (?, ?)
            ON CONFLICT(app_name) DO UPDATE SET
                group_name = excluded.group_name
            """,
            (app_key, group_key),
        )
        await conn.commit()
    log.info("app_groups.set", app_name=app_key, group_name=group_key)


async def get_group(app_name: str) -> str | None:
    """Return the stored group for ``app_name`` or ``None`` when unset."""
    key = app_name.strip()
    if not key:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT group_name FROM app_group WHERE app_name = ?",
            (key,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["group_name"])


async def list_all() -> list[dict[str, str]]:
    """Return every stored ``(app_name, group_name)`` pair, ordered by app.

    The admin UI renders one row per item so the operator sees which
    apps already have a category. ``ORDER BY app_name ASC`` keeps the
    page stable across renders even if rows churn.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, group_name FROM app_group "
            "ORDER BY app_name ASC"
        )
        rows = await cursor.fetchall()
    return [
        {
            "app_name": str(row["app_name"]),
            "group_name": str(row["group_name"]),
        }
        for row in rows
    ]


async def delete_group(app_name: str) -> None:
    """Drop the group assignment for ``app_name``. Idempotent — missing rows are fine."""
    key = app_name.strip()
    if not key:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM app_group WHERE app_name = ?",
            (key,),
        )
        await conn.commit()
    log.info("app_groups.deleted", app_name=key)


# ---------------------------------------------------------------------------
# Aggregate: per-group totals over the last N days
# ---------------------------------------------------------------------------


async def totals_by_group(
    days: int = 30,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> list[dict[str, object]]:
    """Sum active seconds + shot counts per group across the last ``days`` days.

    The seconds figure uses the same gap-capped walk as
    :func:`app.time_on_app.app_summary` — adjacent same-app shots within
    ``max_gap_seconds`` contribute the wall gap, everything else
    contributes zero. The walk is performed *per day* so it never
    bridges midnight even when two shots are seconds apart across the
    boundary; per-day buckets are then summed.

    Apps without an ``app_group`` row are excluded entirely. The
    function never invents an "ungrouped" bucket — silence-by-omission
    is the contract. Result is sorted by ``total_seconds`` descending,
    then by ``shots`` descending as a stable tiebreaker.
    """
    if days <= 0:
        return []

    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=days - 1)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, group_name FROM app_group"
        )
        mapping_rows = await cursor.fetchall()
        app_to_group: dict[str, str] = {
            str(r["app_name"]): str(r["group_name"]) for r in mapping_rows
        }

        if not app_to_group:
            log.info(
                "app_groups.totals.empty_mapping",
                days=days,
                start_day=start_day.isoformat(),
                end_day=today.isoformat(),
            )
            return []

        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, app_name, captured_at "
            "FROM screenshots "
            "WHERE DATE(captured_at) >= ? AND DATE(captured_at) <= ? "
            "AND app_name IS NOT NULL AND app_name != '' "
            "ORDER BY day, captured_at",
            (start_day.isoformat(), today.isoformat()),
        )
        raw_rows = await cursor.fetchall()

    # Group by day so the gap walk never bridges midnight boundaries.
    per_day: dict[str, list[tuple[str, str]]] = {}
    for r in raw_rows:
        day_key = str(r["day"])
        per_day.setdefault(day_key, []).append(
            (
                str(r["app_name"]) if r["app_name"] is not None else "",
                str(r["captured_at"]),
            )
        )

    # Fold per-day per-app numbers up by group_name. We walk the gap
    # logic at the app level (a switch between Code.exe and devenv.exe
    # still breaks the gap, even if both are in the ``dev`` group) so
    # the seconds count matches the time-on-app dashboard exactly.
    group_buckets: dict[str, GroupTotal] = {}
    for day_rows in per_day.values():
        day_app_buckets = _walk_day_rows(day_rows, max_gap_seconds)
        for app, bucket in day_app_buckets.items():
            group = app_to_group.get(app)
            if group is None:
                continue
            agg = group_buckets.get(group)
            if agg is None:
                group_buckets[group] = GroupTotal(
                    group_name=group,
                    shots=bucket["shots"],
                    total_seconds=bucket["seconds"],
                )
            else:
                agg["shots"] += bucket["shots"]
                agg["total_seconds"] += bucket["seconds"]

    items: list[dict[str, object]] = [dict(b) for b in group_buckets.values()]
    items.sort(
        key=lambda r: (int(r["total_seconds"]), int(r["shots"])),  # type: ignore[call-overload]
        reverse=True,
    )

    log.info(
        "app_groups.totals.computed",
        days=days,
        start_day=start_day.isoformat(),
        end_day=today.isoformat(),
        groups=len(items),
        total_seconds=sum(int(i["total_seconds"]) for i in items),  # type: ignore[call-overload]
        total_shots=sum(int(i["shots"]) for i in items),  # type: ignore[call-overload]
    )
    return items


# Re-export the gap-walk helpers' type so external tooling that imports
# :class:`AppTime` from here keeps working without reaching into
# :mod:`app.time_on_app` directly.
__all__ = [
    "AppTime",
    "GroupTotal",
    "delete_group",
    "get_group",
    "list_all",
    "set_group",
    "totals_by_group",
]
