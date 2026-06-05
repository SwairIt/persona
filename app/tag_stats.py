"""Per-tag statistics aggregator.

Computes a small bundle of facts about a single tag, looked up by *name*
(matched case-insensitively against ``tags.name``):

* a total screenshot count,
* the earliest and latest capture timestamps the tag has ever been
  attached to,
* the top ``app_name`` values that the tag co-occurs with,
* the top *other* tags that appear on the same screenshots,
* a dense day-by-day count over the trailing 30 days (zero-filled).

The shape returned by :func:`compute_tag_stats` is shared between the
HTML page and the JSON endpoint in :mod:`app.web.routes.tag_stats`, so
both surfaces stay in lockstep without a second SQL pass.

All SQL is parametrised — the tag name is never interpolated into a
query string. The window for ``daily_timeline`` is hard-pinned to 30
days as the task spec requires; if a caller ever needs a configurable
window, add a parameter rather than baking a literal into the query.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_stats")

_TIMELINE_DAYS = 30
_TOP_APPS_LIMIT = 10
_TOP_CO_TAGS_LIMIT = 10
_ISO_DATE_FMT = "%Y-%m-%d"


class TopApp(TypedDict):
    """One row of the ``top_apps`` table."""

    app_name: str
    count: int


class CoOccurringTag(TypedDict):
    """One row of the ``co_occurring`` table."""

    name: str
    count: int


class DailyEntry(TypedDict):
    """One day of the trailing 30-day timeline."""

    date: str
    count: int


class TagStats(TypedDict):
    """Full payload returned by :func:`compute_tag_stats`."""

    tag: str
    total: int
    first_seen: str | None
    last_seen: str | None
    top_apps: list[TopApp]
    co_occurring: list[CoOccurringTag]
    daily_timeline: list[DailyEntry]


def _normalise_tag(tag: str) -> str:
    """Match :func:`app.storage.tags.create_tag` — lowercased, trimmed."""
    return tag.strip().lower()


def _dense_30_days(today: date | None = None) -> list[str]:
    """Return 30 ISO date strings ending at ``today`` (inclusive)."""
    end = today or date.today()
    start = end - timedelta(days=_TIMELINE_DAYS - 1)
    return [
        (start + timedelta(days=offset)).strftime(_ISO_DATE_FMT)
        for offset in range(_TIMELINE_DAYS)
    ]


async def compute_tag_stats(tag: str) -> TagStats:
    """Aggregate per-tag stats: count, first/last seen, co-tags, apps, 30d.

    Returns the assembled :class:`TagStats` mapping. When the tag has no
    rows at all the totals are ``0``, ``first_seen`` / ``last_seen`` are
    ``None``, the lists are empty and ``daily_timeline`` is a fully
    zero-filled 30-day window — the caller's 404 logic lives in the
    route layer (it asks ``tags`` for the row existence separately so
    "tag exists but is empty" stays distinguishable from "tag is
    unknown").
    """
    name = _normalise_tag(tag)
    log.debug("tag_stats.start", tag=name)

    async with get_connection() as conn:
        # Single round trip for total + first/last seen.
        cursor = await conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                MIN(s.captured_at) AS first_seen,
                MAX(s.captured_at) AS last_seen
            FROM screenshot_tags st
            JOIN screenshots s ON s.id = st.screenshot_id
            JOIN tags t ON t.id = st.tag_id
            WHERE t.name = ?
            """,
            (name,),
        )
        head_row = await cursor.fetchone()

        # Top apps for screenshots that carry this tag.
        cursor = await conn.execute(
            """
            SELECT s.app_name AS app_name, COUNT(*) AS n
            FROM screenshot_tags st
            JOIN screenshots s ON s.id = st.screenshot_id
            JOIN tags t ON t.id = st.tag_id
            WHERE t.name = ?
              AND s.app_name IS NOT NULL
              AND s.app_name <> ''
            GROUP BY s.app_name
            ORDER BY n DESC, s.app_name ASC
            LIMIT ?
            """,
            (name, _TOP_APPS_LIMIT),
        )
        apps_rows = await cursor.fetchall()

        # Co-occurring tags: every *other* tag attached to the same
        # screenshots, ranked by how many shots they share.
        cursor = await conn.execute(
            """
            SELECT other_t.name AS name, COUNT(*) AS n
            FROM screenshot_tags st
            JOIN tags t ON t.id = st.tag_id
            JOIN screenshot_tags other_st
                ON other_st.screenshot_id = st.screenshot_id
               AND other_st.tag_id <> st.tag_id
            JOIN tags other_t ON other_t.id = other_st.tag_id
            WHERE t.name = ?
            GROUP BY other_t.name
            ORDER BY n DESC, other_t.name ASC
            LIMIT ?
            """,
            (name, _TOP_CO_TAGS_LIMIT),
        )
        cotag_rows = await cursor.fetchall()

        # Trailing 30 days of per-day counts. SQLite's date() is happy
        # with the ``-29 days`` modifier as a parameter.
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
            (name, f"-{_TIMELINE_DAYS - 1} days"),
        )
        day_rows = await cursor.fetchall()

    total = int(head_row["total"] or 0) if head_row is not None else 0
    first_seen: str | None = None
    last_seen: str | None = None
    if head_row is not None and total > 0:
        raw_first = head_row["first_seen"]
        raw_last = head_row["last_seen"]
        first_seen = str(raw_first) if raw_first is not None else None
        last_seen = str(raw_last) if raw_last is not None else None

    top_apps: list[TopApp] = [
        TopApp(app_name=str(row["app_name"]), count=int(row["n"]))
        for row in apps_rows
    ]

    co_occurring: list[CoOccurringTag] = [
        CoOccurringTag(name=str(row["name"]), count=int(row["n"]))
        for row in cotag_rows
    ]

    counts_by_day: dict[str, int] = {}
    for row in day_rows:
        raw_day = row["day"]
        if raw_day is None:
            continue
        day = str(raw_day)
        try:
            datetime.strptime(day, _ISO_DATE_FMT)
        except ValueError:
            log.warning("tag_stats.bad_day_skipped", tag=name, day=day)
            continue
        counts_by_day[day] = counts_by_day.get(day, 0) + int(row["n"])

    daily_timeline: list[DailyEntry] = [
        DailyEntry(date=iso, count=counts_by_day.get(iso, 0))
        for iso in _dense_30_days()
    ]

    log.info(
        "tag_stats.computed",
        tag=name,
        total=total,
        first_seen=first_seen,
        last_seen=last_seen,
        top_apps=len(top_apps),
        co_occurring=len(co_occurring),
        timeline_days=len(daily_timeline),
    )

    return TagStats(
        tag=name,
        total=total,
        first_seen=first_seen,
        last_seen=last_seen,
        top_apps=top_apps,
        co_occurring=co_occurring,
        daily_timeline=daily_timeline,
    )
