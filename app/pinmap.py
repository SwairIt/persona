"""Screenshot pin-map — every pinned shot grouped on one page by capture month.

The pin-map is a single-glance overview of *every* frame the user has pinned
(``tier = 'pinned'``) so they can never lose track of the screenshots they
explicitly chose to protect from the tier sweep. Frames are clustered by
calendar month (``YYYY-MM``) of ``captured_at``, newest month first, and each
month's shots are themselves sorted newest-first.

The shape of :class:`PinmapPayload` is the single source of truth for both the
HTML page and the JSON endpoint, so the two views can never drift apart.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.routes.thumbnails import thumbnail_url

log = get_logger("persona.pinmap")


class PinmapShot(TypedDict):
    id: int
    captured_at: str
    thumbnail_url: str | None
    app_name: str | None


class PinmapCluster(TypedDict):
    month: str
    shots: list[PinmapShot]


class PinmapPayload(TypedDict):
    clusters: list[PinmapCluster]
    total: int


def _month_key(captured_at: str) -> str:
    """Return ``YYYY-MM`` from a SQLite ``captured_at`` string.

    Falls back to ``"unknown"`` when the value is too short or malformed so a
    single bad row never breaks the whole grouping pass.
    """
    if len(captured_at) >= 7 and captured_at[4] == "-":
        return captured_at[:7]
    return "unknown"


async def build_pinmap() -> PinmapPayload:
    """Return all pinned screenshots grouped by capture month, newest first.

    The query is parametrised (``tier = ?``) — no string interpolation on user
    input ever reaches SQLite. Months are returned in reverse chronological
    order, and shots inside each cluster are sorted newest-first as well.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, thumbnail_path, app_name "
            "FROM screenshots "
            "WHERE tier = ? AND captured_at IS NOT NULL "
            "ORDER BY captured_at DESC",
            ("pinned",),
        )
        rows = await cursor.fetchall()

    buckets: dict[str, list[PinmapShot]] = {}
    for row in rows:
        captured_at = str(row["captured_at"])
        month = _month_key(captured_at)
        thumb_raw = row["thumbnail_path"]
        thumb_url = thumbnail_url(str(thumb_raw)) if thumb_raw is not None else None
        app_raw = row["app_name"]
        app_name = str(app_raw) if app_raw is not None else None
        buckets.setdefault(month, []).append(
            PinmapShot(
                id=int(row["id"]),
                captured_at=captured_at,
                thumbnail_url=thumb_url,
                app_name=app_name,
            )
        )

    # SQL already gave us ``captured_at DESC``, so iteration order of each
    # bucket's list is newest-first by construction. We just need the months
    # themselves in reverse chronological order — sorting the keys is enough
    # because ``YYYY-MM`` is lexicographically ordered. ``"unknown"`` sorts
    # last via its alphabetic key, which is what we want.
    ordered_months = sorted(
        (m for m in buckets if m != "unknown"),
        reverse=True,
    )
    if "unknown" in buckets:
        ordered_months.append("unknown")

    clusters: list[PinmapCluster] = [
        PinmapCluster(month=month, shots=buckets[month]) for month in ordered_months
    ]
    total = sum(len(c["shots"]) for c in clusters)

    log.info(
        "pinmap.built",
        total=total,
        cluster_count=len(clusters),
    )

    return PinmapPayload(clusters=clusters, total=total)
