"""v0.77 — share-link analytics aggregation.

Every successful GET to ``/shot/share/{shot_id}/{token}`` writes one row
into the v0.55 ``share_visit`` table (see
:file:`app/storage/migrations/055_share_visits.sql`). That journal is
already exposed verbatim by :mod:`app.web.routes.share_visits_csv` for
CSV export, but the owner has no aggregate view: how many opens did a
given shot receive? Which IP-prefix bucket dominates? Does the daily
opens timeline trend up or down?

This module supplies the three roll-ups the ``/admin/share-analytics``
dashboard renders:

* **top_shots** — most-visited shots in the window, joined back to
  ``screenshots`` so the dashboard can show ``app_name`` and the original
  ``captured_at`` next to the visit count.
* **top_ip_prefixes** — top first-two-octets buckets (``ip_prefix`` from
  the v0.55 migration). Empty prefixes are excluded — the v0.55 viewer
  records ``NULL``/``""`` for visitors whose IP we couldn't parse, and a
  blank bucket isn't actionable.
* **daily** — visit count per UTC day, oldest → newest, gaps filled with
  zeros so the SVG line chart's x-axis stays uniform.

All three queries share the same ``days`` window and use parametrised
SQL — the integer flows in as a bound parameter through SQLite's
``datetime('now', ?)`` modifier so we never interpolate it into the
query string. ``days`` is clamped to ``[1, 365]`` to match the rest of
the ``/stats/*`` family and avoid an unbounded scan against a future
high-volume journal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.share_analytics")

# Window bounds — mirror :mod:`app.storage_savings` and
# :mod:`app.web.routes.share_visits_csv` so the operator gets a
# predictable feel across the dashboards.
_MIN_DAYS = 1
_MAX_DAYS = 365
_DEFAULT_DAYS = 30

# How many rows to surface in the "top" lists. Kept small so the
# dashboard's 3-column layout stays scannable; the CSV export remains
# the path for full-fidelity drill-down.
_TOP_SHOTS_LIMIT = 20
_TOP_IPS_LIMIT = 20


class TopShot(TypedDict):
    """One row in the ``top_shots`` list, sorted by ``visits`` desc.

    ``app`` and ``captured_at`` come from the ``screenshots`` row joined
    on ``share_visit.shot_id``. They may be ``None`` if the screenshot
    was hard-deleted after the visit was recorded — v0.55 deliberately
    keeps the journal FK-free so we surface ``None`` and let the
    template render a placeholder rather than dropping the row.
    """

    shot_id: int
    app: str | None
    captured_at: str | None
    visits: int


class TopIpPrefix(TypedDict):
    """One row in the ``top_ip_prefixes`` list, sorted by ``count`` desc."""

    ip: str
    count: int


class DailyVisits(TypedDict):
    """One day's visit count, oldest first when listed by :func:`compute_share_analytics`."""

    date: str
    visits: int


class ShareAnalytics(TypedDict):
    """Composite payload returned by :func:`compute_share_analytics`."""

    top_shots: list[TopShot]
    top_ip_prefixes: list[TopIpPrefix]
    daily: list[DailyVisits]


def _clamp_days(days: int) -> int:
    """Clamp ``days`` to ``[_MIN_DAYS, _MAX_DAYS]`` — see module docstring."""
    return max(_MIN_DAYS, min(int(days), _MAX_DAYS))


async def compute_share_analytics(days: int = _DEFAULT_DAYS) -> ShareAnalytics:
    """Roll up the last ``days`` days of ``share_visit`` rows.

    Returns the dashboard's three lists in a single payload so the
    HTML page and the JSON endpoint share one source of truth. Empty
    inputs produce empty ``top_shots`` / ``top_ip_prefixes`` and a
    zero-filled ``daily`` axis — the dashboard renders an explicit
    "no visits yet" placeholder for the empty case.
    """
    capped = _clamp_days(days)
    since_modifier = f"-{capped} days"

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=capped - 1)

    async with get_connection() as conn:
        # ---- top_shots ----
        # LEFT JOIN so a visit whose screenshots row was hard-deleted
        # still appears in the leaderboard with ``app`` / ``captured_at``
        # = NULL (v0.55 keeps the journal FK-free on purpose).
        cursor = await conn.execute(
            """
            SELECT sv.shot_id        AS shot_id,
                   s.app_name        AS app,
                   s.captured_at     AS captured_at,
                   COUNT(*)          AS visits
            FROM share_visit AS sv
            LEFT JOIN screenshots AS s ON s.id = sv.shot_id
            WHERE sv.visited_at >= datetime('now', ?)
            GROUP BY sv.shot_id
            ORDER BY visits DESC, sv.shot_id ASC
            LIMIT ?
            """,
            (since_modifier, _TOP_SHOTS_LIMIT),
        )
        shot_rows = await cursor.fetchall()

        # ---- top_ip_prefixes ----
        # Filter out NULL and empty prefixes — both are legitimate
        # outputs of the v0.55 viewer when the X-Forwarded-For chain
        # didn't yield a parseable address, but they don't make a
        # useful bucket.
        cursor = await conn.execute(
            """
            SELECT ip_prefix AS ip, COUNT(*) AS count
            FROM share_visit
            WHERE visited_at >= datetime('now', ?)
              AND ip_prefix IS NOT NULL
              AND ip_prefix != ''
            GROUP BY ip_prefix
            ORDER BY count DESC, ip_prefix ASC
            LIMIT ?
            """,
            (since_modifier, _TOP_IPS_LIMIT),
        )
        ip_rows = await cursor.fetchall()

        # ---- daily ----
        # DATE() on the SQLite-stored ``YYYY-MM-DD HH:MM:SS`` produces
        # a clean ``YYYY-MM-DD`` we can match against the Python axis
        # below to fill in zero-visit days.
        cursor = await conn.execute(
            """
            SELECT DATE(visited_at) AS day, COUNT(*) AS visits
            FROM share_visit
            WHERE visited_at >= datetime('now', ?)
            GROUP BY day
            ORDER BY day ASC
            """,
            (since_modifier,),
        )
        day_rows = await cursor.fetchall()

    top_shots: list[TopShot] = [
        TopShot(
            shot_id=int(row["shot_id"]),
            app=(str(row["app"]) if row["app"] is not None else None),
            captured_at=(
                str(row["captured_at"]) if row["captured_at"] is not None else None
            ),
            visits=int(row["visits"]),
        )
        for row in shot_rows
    ]

    top_ip_prefixes: list[TopIpPrefix] = [
        TopIpPrefix(ip=str(row["ip"]), count=int(row["count"])) for row in ip_rows
    ]

    by_day: dict[str, int] = {str(row["day"]): int(row["visits"]) for row in day_rows}
    daily: list[DailyVisits] = []
    cursor_day: date = start_day
    while cursor_day <= today:
        key = cursor_day.isoformat()
        daily.append(DailyVisits(date=key, visits=by_day.get(key, 0)))
        cursor_day += timedelta(days=1)

    log.info(
        "share_analytics.computed",
        days=capped,
        top_shots=len(top_shots),
        top_ip_prefixes=len(top_ip_prefixes),
        daily_days=len(daily),
        total_visits=sum(row["visits"] for row in daily),
    )

    return ShareAnalytics(
        top_shots=top_shots,
        top_ip_prefixes=top_ip_prefixes,
        daily=daily,
    )


__all__ = [
    "DailyVisits",
    "ShareAnalytics",
    "TopIpPrefix",
    "TopShot",
    "compute_share_analytics",
]
