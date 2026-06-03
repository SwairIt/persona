"""Search query usage stats — top queries by frequency from ``search_history``.

v1.4 surfaces the existing ``search_history`` table (introduced in v0.21,
migration ``016_search_history.sql``) as an operator-facing leaderboard:
which queries did the user actually run, how often, and when last.

The schema is ``(query PRIMARY KEY, mode, last_used_at, use_count)`` — a
single row per distinct query that ``app.storage.search_history`` keeps
up-to-date on every search. We never see the per-search timeline (the
table is *aggregated* by design, since v0.21 only kept the last 50 rows
trimmed by ``last_used_at``), so this module's "top by count" view is the
strongest signal available without a schema change.

Why a ``days`` window when the table is already capped at 50 rows? Two
reasons. First, the cap is on ``last_used_at`` order, so an old query
that happens to fall inside the window can still appear — filtering by
``last_used_at >= now - N days`` gives a true "recent usage" view that
won't include queries the user has long since stopped running. Second,
when future migrations grow the cap (or drop it), this module already
honours the window and won't suddenly start surfacing year-old queries.

All SQL is parametrised; the ``days`` and ``limit`` ints are bound, not
formatted. Returned rows are plain dicts so the route layer can shape
them for both HTML and JSON without further unwrapping.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.search.query_stats")

# Mirror :data:`app.storage.search_history._MAX_ROWS` — the table is capped
# at 50 rows, so requesting more than that is meaningless even with a wide
# window. The route layer also clamps the query parameter so a bad caller
# can't bypass the limit by binding a huge int.
_MAX_LIMIT = 50
_DEFAULT_LIMIT = 50
_DEFAULT_DAYS = 30


class TopQuery(TypedDict):
    """One row of the top-queries leaderboard.

    * ``query`` — the normalised search string as the user typed it
      (``app.storage.search_history.record_query`` strips surrounding
      whitespace before insert, so we never need to re-trim here).
    * ``count`` — value of ``use_count`` at read time. Monotonically
      increasing per query; never reset.
    * ``last_used`` — ISO-8601 timestamp (UTC, ``YYYY-MM-DD HH:MM:SS``)
      from SQLite's ``datetime('now')`` default. Always present — the
      column is ``NOT NULL`` with a default in the migration.
    """

    query: str
    count: int
    last_used: str


async def top_queries(
    days: int = _DEFAULT_DAYS,
    limit: int = _DEFAULT_LIMIT,
) -> list[TopQuery]:
    """Return the most-used queries in the last ``days``, capped at ``limit``.

    Sorted by ``use_count`` descending, ties broken by ``last_used_at``
    descending so the freshest of equally-popular queries floats first.

    ``days`` < 1 is clamped to 1 (a zero-day window would always return
    an empty list — likely a caller bug, not user intent). ``limit`` is
    clamped to ``_MAX_LIMIT`` for the same reason: silently returning
    less than the caller asked for is preferable to running a query that
    can't return more than 50 rows anyway.
    """
    safe_days = max(1, int(days))
    safe_limit = max(1, min(_MAX_LIMIT, int(limit)))

    # The ``-N days`` modifier is interpolated into the SQL string itself
    # rather than bound — SQLite's ``datetime()`` rejects parameter-bound
    # modifiers, and we control the value end-to-end (cast to int above).
    # Everything that *could* come from a caller is still bound.
    window_modifier = f"-{safe_days} days"

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT query, use_count, last_used_at
            FROM search_history
            WHERE last_used_at >= datetime('now', ?)
            ORDER BY use_count DESC, last_used_at DESC
            LIMIT ?
            """,
            (window_modifier, safe_limit),
        )
        rows = await cursor.fetchall()

    result: list[TopQuery] = [
        {
            "query": str(row["query"]),
            "count": int(row["use_count"]),
            "last_used": str(row["last_used_at"]),
        }
        for row in rows
    ]

    log.info(
        "search.query_stats.computed",
        days=safe_days,
        limit=safe_limit,
        rows=len(result),
    )
    return result
