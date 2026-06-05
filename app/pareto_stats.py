"""Pareto analysis of screenshot capture counts per ``app_name``.

The classic 80/20 lens applied to the operator's own usage telemetry —
"which 20% of apps account for 80% of every captured moment". A bar
chart sorted in descending order combines naturally with the cumulative
share curve to expose the inflection point where the long tail begins.

The aggregation is deliberately on ``COUNT(*) FROM screenshots`` rather
than on a derived "minutes of attention" figure: the capture cadence is
near-uniform across foreground apps, so shot count is a faithful proxy
for attention spent — and it requires no additional joins, no NULL
gymnastics around ``focus_seconds`` columns that don't always exist on
older shots, and no time-zone bookkeeping for an aggregate that is, by
construction, time-zone invariant inside its rolling window.

Two thresholds are reported:

* ``threshold_index`` — the zero-based index of the first app whose
  cumulative share crosses the 80% line. ``-1`` when the window is
  empty (no shots in the requested window).
* ``threshold_count`` — the count of apps up to and including that
  threshold index (i.e. ``threshold_index + 1``), pre-computed so the
  template can render "X apps = 80% of your time" without an off-by-one
  in Jinja.
"""

from __future__ import annotations

from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.pareto_stats")

# Pareto's namesake threshold. Exposed as a module constant so a future
# tweak (e.g. an operator who wants the 90% inflection instead) can be
# made by editing one line — the route, the template summary copy, and
# every test all read this single source of truth.
_PARETO_THRESHOLD: Final[float] = 80.0

# ``DATE('now', '-{days} days')`` is the SQLite idiom for "midnight, N
# days ago" used elsewhere in the codebase (see ``app_stats.apps_index``
# for a similar 14-day call). We bind both literals as parameters so the
# query stays fully parameterised — no f-string interpolation into SQL.
_DATE_MODIFIER_NOW: Final[str] = "now"


async def compute_app_pareto(days: int = 30) -> dict[str, Any]:
    """Return the per-app shot count, cumulative share, and 80% threshold.

    Args:
        days: Rolling window in days. The query uses ``DATE('now',
            '-{days} days')`` so the cutoff is "midnight, N days ago"
            in SQLite's local-time semantics — matching the convention
            of every other ``/stats/*`` page.

    Returns:
        A dictionary with the shape documented in the module header:

        ``{
            "days": int,
            "total_apps": int,
            "total_shots": int,
            "apps": [
                {
                    "app": str,
                    "shots": int,
                    "percent_individual": float,
                    "percent_cumulative": float,
                },
                ...
            ],
            "threshold_index": int,   # ``-1`` when the window is empty
            "threshold_count": int,   # ``0`` when the window is empty
        }``

        The ``apps`` list is sorted by ``shots`` descending; percentages
        are rounded to two decimal places so the template can render
        them without further formatting and JSON consumers get a stable
        representation.
    """
    # SQLite's date modifier is concatenated *inside* the date function
    # from the bound parameters — neither leaks into the SQL string
    # itself, so the call site stays parameterised even though ``days``
    # is an integer rather than a free-text user input.
    offset_modifier = f"-{int(days)} days"

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name AS app_name, COUNT(*) AS shots "
            "FROM screenshots "
            "WHERE app_name IS NOT NULL "
            "  AND app_name != '' "
            "  AND captured_at >= DATE(?, ?) "
            "GROUP BY app_name "
            "ORDER BY shots DESC",
            (_DATE_MODIFIER_NOW, offset_modifier),
        )
        # ``aiosqlite`` types ``fetchall`` as ``Iterable[Row]`` rather
        # than ``Sequence[Row]``, so we materialise into a list once for
        # the two-pass walk (one ``sum`` for the total, one ``enumerate``
        # for the cumulative-share projection).
        rows = list(await cursor.fetchall())

    total_shots = sum(int(row["shots"]) for row in rows)
    total_apps = len(rows)

    apps: list[dict[str, Any]] = []
    threshold_index = -1
    running_total = 0

    for idx, row in enumerate(rows):
        shots = int(row["shots"])
        running_total += shots
        if total_shots > 0:
            percent_individual = round(shots / total_shots * 100.0, 2)
            percent_cumulative = round(running_total / total_shots * 100.0, 2)
        else:
            # Defensive: ``rows`` is empty when ``total_shots == 0``, so
            # this branch is never entered in practice. Keep the explicit
            # zero so a future contributor cannot trip on a ZeroDivision
            # while spelunking through partial-window edge cases.
            percent_individual = 0.0
            percent_cumulative = 0.0
        apps.append(
            {
                "app": str(row["app_name"]),
                "shots": shots,
                "percent_individual": percent_individual,
                "percent_cumulative": percent_cumulative,
            }
        )
        if threshold_index == -1 and percent_cumulative >= _PARETO_THRESHOLD:
            threshold_index = idx

    threshold_count = threshold_index + 1 if threshold_index >= 0 else 0

    log.info(
        "pareto.computed",
        days=days,
        total_apps=total_apps,
        total_shots=total_shots,
        threshold_index=threshold_index,
        threshold_count=threshold_count,
    )

    return {
        "days": days,
        "total_apps": total_apps,
        "total_shots": total_shots,
        "apps": apps,
        "threshold_index": threshold_index,
        "threshold_count": threshold_count,
    }
