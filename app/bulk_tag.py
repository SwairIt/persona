"""Bulk tag/untag operations driven by an FTS5 search query.

Used by the CLI subcommands ``tag`` and ``untag`` to apply (or remove) a tag
across every screenshot whose OCR/title/app matches a free-text query.

Reuses :func:`app.search.search` for the FTS5 MATCH so we never re-implement
the FTS5 SQL.

The ``preview_bulk_tag`` / ``apply_bulk_tag`` pair (added later) supports the
admin web UI: it takes a structured filter (``app`` / ``window_contains`` /
``ocr_contains`` / ``date_only``) plus an optional date range and either
shows a non-mutating preview or applies an add/remove action against the
``screenshot_tags`` table (see migration ``001_tags.sql``: columns are
``screenshot_id`` + ``tag_id`` + ``created_at``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypedDict

from app.logging_setup import get_logger
from app.search import search as fts_search
from app.storage.db import get_connection
from app.storage.tags import create_tag, tag_screenshot, untag_screenshot

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.cli.tag")
bulk_log = get_logger("persona.bulk_tag")

FilterKind = Literal["app", "window_contains", "ocr_contains", "date_only"]
ApplyAction = Literal["add", "remove"]

_ALLOWED_FILTER_KINDS: frozenset[str] = frozenset(
    {"app", "window_contains", "ocr_contains", "date_only"}
)
_ALLOWED_ACTIONS: frozenset[str] = frozenset({"add", "remove"})
_PREVIEW_SAMPLE_SIZE = 5
_MATCH_HARD_LIMIT = 100_000


class BulkTagPreview(TypedDict):
    """Outcome of :func:`preview_bulk_tag` — never mutates the DB."""

    count: int
    sample: list[dict[str, Any]]


class BulkTagApplyResult(TypedDict):
    """Outcome of :func:`apply_bulk_tag` — count of rows changed."""

    affected: int
    action: ApplyAction
    tag: str


class BulkTagResult(TypedDict):
    """Outcome summary returned by :func:`bulk_tag` and :func:`bulk_untag`."""

    tag: str
    query: str
    matched: int
    affected: int
    dry_run: bool


async def _resolve_matching_ids(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int,
) -> list[int]:
    """Run the existing FTS5 search and return matched screenshot ids only."""
    hits = await fts_search(conn, query=query, limit=limit)
    return [hit.screenshot_id for hit in hits]


async def bulk_tag(
    tag: str,
    query: str,
    limit: int,
    dry_run: bool,
) -> BulkTagResult:
    """Apply ``tag`` to every screenshot whose FTS5 MATCH on ``query`` succeeds.

    The tag row is created on demand (idempotent). The screenshot ↔ tag link
    is inserted via ``INSERT OR IGNORE`` so calling twice is safe.

    Returns a :class:`BulkTagResult` describing what happened.
    """
    normalised = tag.strip().lower()
    async with get_connection() as conn:
        screenshot_ids = await _resolve_matching_ids(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "bulk_tag.dry_run",
                tag=normalised,
                query=query,
                matched=len(screenshot_ids),
            )
            return BulkTagResult(
                tag=normalised,
                query=query,
                matched=len(screenshot_ids),
                affected=len(screenshot_ids),
                dry_run=True,
            )

        if not screenshot_ids:
            log.info("bulk_tag.empty", tag=normalised, query=query)
            return BulkTagResult(
                tag=normalised,
                query=query,
                matched=0,
                affected=0,
                dry_run=False,
            )

        tag_id = await create_tag(conn, name=normalised)
        for screenshot_id in screenshot_ids:
            await tag_screenshot(conn, screenshot_id, tag_id)

    log.info(
        "bulk_tag.applied",
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
    )
    return BulkTagResult(
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
        affected=len(screenshot_ids),
        dry_run=False,
    )


async def bulk_untag(
    tag: str,
    query: str,
    limit: int,
) -> BulkTagResult:
    """Remove ``tag`` from every screenshot whose FTS5 MATCH on ``query`` succeeds.

    If the tag row does not exist we short-circuit with ``affected=0``.
    """
    normalised = tag.strip().lower()
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (normalised,))
        row = await cursor.fetchone()
        if row is None:
            log.info("bulk_untag.no_tag", tag=normalised, query=query)
            return BulkTagResult(
                tag=normalised,
                query=query,
                matched=0,
                affected=0,
                dry_run=False,
            )
        tag_id = int(row["id"])

        screenshot_ids = await _resolve_matching_ids(conn, query=query, limit=limit)
        for screenshot_id in screenshot_ids:
            await untag_screenshot(conn, screenshot_id, tag_id)

    log.info(
        "bulk_untag.applied",
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
    )
    return BulkTagResult(
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
        affected=len(screenshot_ids),
        dry_run=False,
    )


def _validate_filter_kind(filter_kind: str) -> FilterKind:
    """Reject unknown filter kinds early so SQL is never built blindly."""
    if filter_kind not in _ALLOWED_FILTER_KINDS:
        msg = (
            "filter_kind must be one of "
            "app|window_contains|ocr_contains|date_only, got " + repr(filter_kind)
        )
        raise ValueError(msg)
    # mypy: narrow str → Literal via assertion-cast.
    return filter_kind  # type: ignore[return-value]


def _validate_action(action: str) -> ApplyAction:
    if action not in _ALLOWED_ACTIONS:
        msg = "action must be add|remove, got " + repr(action)
        raise ValueError(msg)
    return action  # type: ignore[return-value]


def _build_filter_clause(
    filter_kind: FilterKind,
    filter_value: str,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[Any]]:
    """Return (WHERE-fragment, params) for the given filter combo.

    All values flow through ``?`` placeholders — no string interpolation
    of user input — so ruff S608 stays clean and SQL injection is
    structurally impossible. The fragment is intentionally a bare
    ``WHERE …`` chain so callers can drop it straight into either a
    ``SELECT`` (preview) or an ``IN (SELECT …)`` (apply).
    """
    clauses: list[str] = []
    params: list[Any] = []

    cleaned = (filter_value or "").strip()

    if filter_kind == "app":
        # Exact app match: avoids "code" leaking into "vscode" etc.
        if not cleaned:
            msg = "filter_value is required for filter_kind=app"
            raise ValueError(msg)
        clauses.append("app_name = ?")
        params.append(cleaned)
    elif filter_kind == "window_contains":
        if not cleaned:
            msg = "filter_value is required for filter_kind=window_contains"
            raise ValueError(msg)
        clauses.append("window_title LIKE ?")
        params.append("%" + cleaned + "%")
    elif filter_kind == "ocr_contains":
        if not cleaned:
            msg = "filter_value is required for filter_kind=ocr_contains"
            raise ValueError(msg)
        # Only rows that actually completed OCR can possibly contain
        # the substring — narrow first, LIKE second.
        clauses.append("ocr_status = 'done'")
        clauses.append("ocr_text LIKE ?")
        params.append("%" + cleaned + "%")
    elif filter_kind == "date_only":
        # ``date_only`` means "any shot in the date range" — no extra
        # text predicate. We still require the range to be non-empty,
        # otherwise a stray click would match every row in the DB.
        if not (date_from or date_to):
            msg = "filter_kind=date_only requires at least one of date_from/date_to"
            raise ValueError(msg)

    if date_from:
        clauses.append("captured_at >= ?")
        params.append(date_from)
    if date_to:
        # Inclusive upper bound — operators think in whole days, so
        # ``date_to = 2026-06-05`` should include all of that day.
        clauses.append("captured_at < datetime(?, '+1 day')")
        params.append(date_to)

    where_sql = " AND ".join(clauses) if clauses else "1=1"
    return where_sql, params


async def _matching_ids(
    conn: aiosqlite.Connection,
    where_sql: str,
    params: list[Any],
    limit: int,
) -> list[int]:
    """Return screenshot ids matching the prepared WHERE clause."""
    sql = f"SELECT id FROM screenshots WHERE {where_sql} ORDER BY captured_at DESC LIMIT ?"  # noqa: S608
    cursor = await conn.execute(sql, [*params, limit])
    rows = await cursor.fetchall()
    return [int(row["id"]) for row in rows]


async def _sample_rows(
    conn: aiosqlite.Connection,
    where_sql: str,
    params: list[Any],
) -> list[dict[str, Any]]:
    """Return up to :data:`_PREVIEW_SAMPLE_SIZE` rows for the preview pane."""
    select_cols = "SELECT id, captured_at, app_name, window_title, ocr_status FROM screenshots"
    sql = f"{select_cols} WHERE {where_sql} ORDER BY captured_at DESC LIMIT ?"
    cursor = await conn.execute(sql, [*params, _PREVIEW_SAMPLE_SIZE])
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "app_name": row["app_name"],
            "window_title": row["window_title"],
            "ocr_status": str(row["ocr_status"]),
        }
        for row in rows
    ]


async def _count_matches(
    conn: aiosqlite.Connection,
    where_sql: str,
    params: list[Any],
) -> int:
    """Cheap COUNT(*) for the preview header — no ids materialised."""
    sql = f"SELECT COUNT(*) AS n FROM screenshots WHERE {where_sql}"  # noqa: S608
    cursor = await conn.execute(sql, params)
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def preview_bulk_tag(
    filter_kind: str,
    filter_value: str,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """Preview which screenshots would be touched by a bulk-tag operation.

    Does **not** mutate any tables. Returns ``{count, sample}`` where
    ``sample`` is the first :data:`_PREVIEW_SAMPLE_SIZE` matching rows as
    plain dicts (id + captured_at + app_name + window_title + ocr_status),
    so the admin page can render a confidence-building snapshot before the
    operator hits ``Apply``.

    ``filter_kind`` must be one of ``app`` / ``window_contains`` /
    ``ocr_contains`` / ``date_only``. ``date_from`` and ``date_to`` are
    ISO-ish date strings (``YYYY-MM-DD``); the upper bound is inclusive.
    """
    kind = _validate_filter_kind(filter_kind)
    where_sql, params = _build_filter_clause(kind, filter_value, date_from, date_to)
    async with get_connection() as conn:
        count = await _count_matches(conn, where_sql, params)
        sample = await _sample_rows(conn, where_sql, params)

    bulk_log.info(
        "preview",
        filter_kind=kind,
        filter_value=filter_value,
        date_from=date_from,
        date_to=date_to,
        count=count,
    )
    return {"count": count, "sample": sample}


async def apply_bulk_tag(
    filter_kind: str,
    filter_value: str,
    date_from: str | None,
    date_to: str | None,
    tag: str,
    action: str,
) -> dict[str, Any]:
    """Apply or remove ``tag`` across every shot matching the filter.

    * ``action="add"`` — ``INSERT OR IGNORE`` into ``screenshot_tags`` so
      already-tagged shots stay put (SQLite's spelling of the requested
      ``ON CONFLICT DO NOTHING`` — the underlying primary-key conflict
      target is the same).
    * ``action="remove"`` — ``DELETE FROM screenshot_tags WHERE tag_id = ?
      AND screenshot_id IN (matching ids)``. The migration column is
      ``tag_id`` (not ``tag``) — we resolve the tag name to its row id
      first; missing tag = no-op (``affected=0``).

    Returns ``{affected, action, tag}``. Tag name is lower-cased to match
    the normalisation used by :func:`app.storage.tags.create_tag`.
    """
    kind = _validate_filter_kind(filter_kind)
    act = _validate_action(action)
    normalised_tag = (tag or "").strip().lower()
    if not normalised_tag:
        msg = "tag must be non-empty"
        raise ValueError(msg)

    where_sql, params = _build_filter_clause(kind, filter_value, date_from, date_to)

    async with get_connection() as conn:
        screenshot_ids = await _matching_ids(conn, where_sql, params, _MATCH_HARD_LIMIT)
        if not screenshot_ids:
            bulk_log.info(
                "apply.empty",
                action=act,
                tag=normalised_tag,
                filter_kind=kind,
            )
            return {"affected": 0, "action": act, "tag": normalised_tag}

        if act == "add":
            tag_id = await create_tag(conn, name=normalised_tag)
            affected = 0
            for screenshot_id in screenshot_ids:
                # ``tag_screenshot`` uses INSERT OR IGNORE under the hood,
                # which is SQLite's idiom for "ON CONFLICT DO NOTHING".
                # rowcount is unreliable across drivers, so we re-derive
                # the affected count from the matched id set — every id
                # that didn't already carry the tag is one row inserted.
                await tag_screenshot(conn, screenshot_id, tag_id)
            # Count rows that now carry the tag *and* were in our match
            # set — that's the true "affected" number callers expect.
            placeholders = ",".join("?" * len(screenshot_ids))
            in_clause = f"screenshot_id IN ({placeholders})"
            count_sql = (
                f"SELECT COUNT(*) AS n FROM screenshot_tags WHERE tag_id = ? AND {in_clause}"  # noqa: S608
            )
            cursor = await conn.execute(count_sql, [tag_id, *screenshot_ids])
            row = await cursor.fetchone()
            affected = int(row["n"]) if row is not None else 0
        else:  # act == "remove"
            cursor = await conn.execute(
                "SELECT id FROM tags WHERE name = ?",
                (normalised_tag,),
            )
            row = await cursor.fetchone()
            if row is None:
                bulk_log.info(
                    "apply.no_tag",
                    action=act,
                    tag=normalised_tag,
                )
                return {"affected": 0, "action": act, "tag": normalised_tag}
            tag_id = int(row["id"])
            placeholders = ",".join("?" * len(screenshot_ids))
            in_clause = f"screenshot_id IN ({placeholders})"
            delete_sql = (
                f"DELETE FROM screenshot_tags WHERE tag_id = ? AND {in_clause}"  # noqa: S608
            )
            delete_cursor = await conn.execute(delete_sql, [tag_id, *screenshot_ids])
            affected = int(delete_cursor.rowcount or 0)
            await conn.commit()

    bulk_log.info(
        "apply.done",
        action=act,
        tag=normalised_tag,
        filter_kind=kind,
        affected=affected,
    )
    return {"affected": affected, "action": act, "tag": normalised_tag}


__all__ = [
    "ApplyAction",
    "BulkTagApplyResult",
    "BulkTagPreview",
    "BulkTagResult",
    "FilterKind",
    "apply_bulk_tag",
    "bulk_tag",
    "bulk_untag",
    "preview_bulk_tag",
]
