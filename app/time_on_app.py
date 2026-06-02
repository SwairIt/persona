"""Time-on-app dashboard — gap-capped per-app active seconds from screenshots.

Definition of "time spent in app X":

    Walk the day's screenshots in chronological order.  For each *adjacent
    pair* (prev, curr) where ``prev.app_name == curr.app_name`` and the wall
    gap between them is ``<= max_gap_seconds``, add that gap to X's bucket.
    Any wider gap, or a switch to a different app, contributes nothing — we
    treat it as "user was idle / capture paused" rather than continuous
    presence in the app.

Shot counts are exact (every screenshot increments its app's ``shots`` by
one); only the seconds bucket is gap-aware.

This is deliberately a thin, dict-returning surface kept separate from the
existing :mod:`app.analysis.time_sheet`, which uses a different attribution
model (single-shot ticks).  Both can coexist.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.time_on_app")

DEFAULT_MAX_GAP_SECONDS = 300


class AppTime(TypedDict):
    app_name: str
    seconds: int
    shots: int


def _walk_day_rows(
    rows: list[tuple[str, str]],
    max_gap_seconds: int,
) -> dict[str, AppTime]:
    """Fold ``[(app_name, captured_at_iso), ...]`` into per-app buckets.

    Rows are assumed to be already ordered by ``captured_at`` ASC.
    """
    buckets: dict[str, AppTime] = {}
    prev_app: str | None = None
    prev_dt: datetime | None = None

    for app_raw, captured_at_raw in rows:
        if not app_raw:
            # Capture without a known foreground app — skip entirely;
            # it can't be attributed.
            prev_app = None
            prev_dt = None
            continue

        app = str(app_raw)
        when = datetime.fromisoformat(str(captured_at_raw))

        bucket = buckets.get(app)
        if bucket is None:
            bucket = AppTime(app_name=app, seconds=0, shots=0)
            buckets[app] = bucket
        bucket["shots"] += 1

        if prev_app == app and prev_dt is not None:
            diff = (when - prev_dt).total_seconds()
            if 0 < diff <= max_gap_seconds:
                bucket["seconds"] += int(diff)
        prev_app = app
        prev_dt = when

    return buckets


async def daily_time_on_app(
    day_iso: str,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> list[dict[str, object]]:
    """Return per-app active-seconds + shot count for the given local day.

    ``day_iso`` is a ``YYYY-MM-DD`` string.  Result is sorted by ``seconds``
    descending, then by ``shots`` descending (stable tiebreaker for apps
    that have only solo shots and thus zero seconds).
    """
    try:
        target = date.fromisoformat(day_iso)
    except ValueError:
        log.warning("time_on_app.bad_day_iso", day_iso=day_iso)
        target = datetime.now().astimezone().date()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, captured_at FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "ORDER BY captured_at",
            (target.isoformat(),),
        )
        raw_rows = await cursor.fetchall()

    rows: list[tuple[str, str]] = [
        (str(r["app_name"]) if r["app_name"] is not None else "", str(r["captured_at"]))
        for r in raw_rows
    ]
    buckets = _walk_day_rows(rows, max_gap_seconds)

    items: list[dict[str, object]] = [dict(b) for b in buckets.values()]
    items.sort(
        key=lambda r: (int(r["seconds"]), int(r["shots"])),  # type: ignore[call-overload]
        reverse=True,
    )

    log.info(
        "time_on_app.computed",
        day=target.isoformat(),
        apps=len(items),
        total_seconds=sum(int(i["seconds"]) for i in items),  # type: ignore[call-overload]
        total_shots=sum(int(i["shots"]) for i in items),  # type: ignore[call-overload]
        max_gap_seconds=max_gap_seconds,
    )
    return items


async def app_summary(
    days: int = 7,
    max_gap_seconds: int = DEFAULT_MAX_GAP_SECONDS,
) -> list[dict[str, object]]:
    """Aggregate per-app totals across the last ``days`` days (inclusive).

    Today is always included; the window is ``[today - days + 1, today]``.
    The gap walk is performed *per day* (we never bridge across midnight
    even if two shots are seconds apart), then summed.
    """
    if days <= 0:
        return []

    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=days - 1)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, app_name, captured_at "
            "FROM screenshots "
            "WHERE DATE(captured_at) >= ? AND DATE(captured_at) <= ? "
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

    totals: dict[str, AppTime] = {}
    for day_rows in per_day.values():
        day_buckets = _walk_day_rows(day_rows, max_gap_seconds)
        for app, bucket in day_buckets.items():
            agg = totals.get(app)
            if agg is None:
                totals[app] = AppTime(
                    app_name=app,
                    seconds=bucket["seconds"],
                    shots=bucket["shots"],
                )
            else:
                agg["seconds"] += bucket["seconds"]
                agg["shots"] += bucket["shots"]

    items: list[dict[str, object]] = [dict(b) for b in totals.values()]
    items.sort(
        key=lambda r: (int(r["seconds"]), int(r["shots"])),  # type: ignore[call-overload]
        reverse=True,
    )

    log.info(
        "time_on_app.summary",
        days=days,
        start_day=start_day.isoformat(),
        end_day=today.isoformat(),
        apps=len(items),
        total_seconds=sum(int(i["seconds"]) for i in items),  # type: ignore[call-overload]
        max_gap_seconds=max_gap_seconds,
    )
    return items
