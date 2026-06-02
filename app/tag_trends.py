"""Per-tag day-by-day trend over a trailing window.

Joins :class:`screenshot_tags` with :class:`screenshots` and groups by
``DATE(captured_at)`` for a single tag (looked up by *name* — the tag id is
an internal detail the caller shouldn't need to know about). Always returns
a dense list — every day in the window is present, even with ``count = 0``
— so the SVG sparkline and the HTML table never need missing-key guards.

The output is a plain ``list[TagTrendEntry]`` shared by the HTML page and
the JSON endpoint, keeping a single source of truth for the shape.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_trends")

_MIN_DAYS = 1
_MAX_DAYS = 3650  # ~10 years — keeps the window safely bounded.
_ISO_DATE_FMT = "%Y-%m-%d"


class TagTrendEntry(TypedDict):
    date: str
    count: int


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _normalise_tag(tag: str) -> str:
    """Match :func:`app.storage.tags.create_tag` — lowercased, trimmed."""
    return tag.strip().lower()


def _dense_window(days: int, today: date | None = None) -> list[str]:
    """Return ``days`` ISO date strings ending at ``today`` (inclusive)."""
    end = today or date.today()
    start = end - timedelta(days=days - 1)
    return [
        (start + timedelta(days=offset)).strftime(_ISO_DATE_FMT)
        for offset in range(days)
    ]


async def tag_trend(tag: str, days: int = 30) -> list[TagTrendEntry]:
    """Return a dense ``days``-entry list of ``{date, count}`` for ``tag``.

    * ``tag`` is matched case-insensitively against ``tags.name``.
    * Every day in the trailing window is present — missing days get
      ``count = 0`` so callers can iterate without a guard.
    * An unknown tag returns the dense window with all-zero counts; the
      route layer treats that as a 404 by checking the tag's existence
      separately when it needs to.
    """
    window = _clamp_days(days)
    modifier = f"-{window - 1} days"
    name = _normalise_tag(tag)

    counts: dict[str, int] = {}
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT DATE(s.captured_at) AS day, COUNT(*) AS n
            FROM screenshot_tags st
            JOIN screenshots s ON s.id = st.screenshot_id
            JOIN tags t ON t.id = st.tag_id
            WHERE t.name = ?
              AND s.captured_at IS NOT NULL
              AND DATE(s.captured_at) >= DATE('now', ?)
            GROUP BY day
            ORDER BY day
            """,
            (name, modifier),
        )
        raw_rows = await cursor.fetchall()

    for row in raw_rows:
        raw_day = row["day"]
        if raw_day is None:
            continue
        day = str(raw_day)
        try:
            datetime.strptime(day, _ISO_DATE_FMT)
        except ValueError:
            log.warning("tag_trends.bad_day_skipped", tag=name, day=day)
            continue
        counts[day] = counts.get(day, 0) + int(row["n"])

    dense: list[TagTrendEntry] = [
        TagTrendEntry(date=iso, count=counts.get(iso, 0))
        for iso in _dense_window(window)
    ]

    total = sum(entry["count"] for entry in dense)
    log.info(
        "tag_trends.computed",
        tag=name,
        days=window,
        total=total,
        non_zero_days=sum(1 for entry in dense if entry["count"] > 0),
    )

    return dense
