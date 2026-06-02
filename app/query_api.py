"""Structured JSON query over screenshots, notes, tags and days.

Single entry point :func:`run_query` accepts a typed query dict and returns
mixed results bucketed by ``kind``. It is intentionally a thin orchestration
layer:

* FTS5 over OCR / window-title / app-name reuses :func:`app.search.search` —
  this module never touches the ``screenshots_fts`` table directly so the
  sanitisation + ``snippet()`` formatting stay in one place.
* FTS5 over screenshot notes reuses the helper in
  :mod:`app.web.routes.notes_search` for the same reason — that module owns
  the encryption-leak audit and the FTS5 special-character escape list.
* Tags and per-day aggregates run their own narrow SELECTs against
  ``tags`` / ``screenshots`` with bind parameters only; no user input is
  ever interpolated into SQL strings.

The function is read-only (no commits) and caps every per-kind result list
at the caller-supplied ``limit`` (default 50, hard-capped at 500) to keep
response sizes bounded.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from app.logging_setup import get_logger
from app.search import search as run_screenshot_search
from app.storage.db import get_connection
from app.storage.time import iso as _iso
from app.web.routes.notes_search import _run_search as _run_notes_fts_search

log = get_logger("persona.query_api")

# Hard ceiling so a misbehaving caller can't request a million rows. The
# pydantic model in the route layer enforces the same ceiling, but we
# re-check here because :func:`run_query` is also reachable from internal
# Python callers (CLI / background workers) which bypass the route model.
_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50

Kind = Literal["screenshot", "note", "tag", "day"]
_ALL_KINDS: tuple[Kind, ...] = ("screenshot", "note", "tag", "day")


def _coerce_kinds(raw: list[str] | None) -> list[Kind]:
    """Validate ``kinds`` list, dedupe while preserving order.

    Empty / missing → all four kinds. Unknown strings raise ``ValueError``
    so the caller (route or in-process) sees a precise failure rather than
    a silently dropped kind.
    """
    if not raw:
        return list(_ALL_KINDS)
    seen: set[str] = set()
    out: list[Kind] = []
    for entry in raw:
        if entry in seen:
            continue
        if entry not in _ALL_KINDS:
            msg = f"Unknown kind: {entry!r} (allowed: {list(_ALL_KINDS)})"
            raise ValueError(msg)
        seen.add(entry)
        out.append(entry)
    return out


def _coerce_limit(raw: int | None) -> int:
    """Clamp ``limit`` into ``[1, _MAX_LIMIT]`` with a sane default."""
    if raw is None:
        return _DEFAULT_LIMIT
    if raw < 1:
        return 1
    if raw > _MAX_LIMIT:
        return _MAX_LIMIT
    return int(raw)


def _parse_date(value: str | None) -> datetime | None:
    """Parse ``YYYY-MM-DD`` or full ISO 8601 into a tz-aware ``datetime``.

    ``None`` / empty / whitespace → ``None`` (no filter). Invalid input
    raises ``ValueError`` — the route layer translates that into 400.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    # Accept bare YYYY-MM-DD by upgrading to start-of-day UTC. Anything
    # else is delegated to ``datetime.fromisoformat`` which handles full
    # ISO timestamps including a ``Z`` suffix on Python 3.12.
    if len(stripped) == 10 and stripped[4] == "-" and stripped[7] == "-":
        parsed_date = date.fromisoformat(stripped)
        return datetime(
            parsed_date.year,
            parsed_date.month,
            parsed_date.day,
            tzinfo=UTC,
        )
    parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _normalise_tags(raw: list[str] | None) -> list[str]:
    """Lowercase, strip, dedupe-preserving-order. Empty entries dropped."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        cleaned = (entry or "").strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


async def _search_screenshots(
    conn: Any,
    *,
    fts: str | None,
    app_name: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    tags: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Reuse :func:`app.search.search` then post-filter by tag membership."""
    hits = await run_screenshot_search(
        conn,
        query=fts or "",
        limit=limit if not tags else min(_MAX_LIMIT, limit * 4),
        since=date_from,
        until=date_to,
        app_name=app_name,
    )
    if not hits:
        return []

    out_rows: list[dict[str, Any]] = [
        {
            "id": hit.screenshot_id,
            "captured_at": hit.captured_at.isoformat(),
            "thumbnail_path": hit.thumbnail_path,
            "app_name": hit.app_name,
            "window_title": hit.window_title,
            "snippet": hit.snippet,
            "rank": hit.rank,
        }
        for hit in hits
    ]

    if tags:
        ids = [int(row["id"]) for row in out_rows]
        placeholders = ",".join("?" * len(ids))
        tag_placeholders = ",".join("?" * len(tags))
        # Match every requested tag (AND semantics) by counting distinct
        # matches per screenshot. ``placeholders`` / ``tag_placeholders``
        # are derived from list *lengths* only — they contain nothing
        # but ``?,?,?`` so the S608 warning is a false positive here.
        in_ids = f"({placeholders})"
        in_tags = f"({tag_placeholders})"
        sql = (
            f"SELECT st.screenshot_id AS sid FROM screenshot_tags st "  # noqa: S608
            f"JOIN tags t ON t.id = st.tag_id "
            f"WHERE st.screenshot_id IN {in_ids} AND t.name IN {in_tags} "
            f"GROUP BY st.screenshot_id HAVING COUNT(DISTINCT t.name) = ?"
        )
        cursor = await conn.execute(sql, [*ids, *tags, len(tags)])
        rows = await cursor.fetchall()
        keep = {int(row["sid"]) for row in rows}
        out_rows = [row for row in out_rows if int(row["id"]) in keep]

    return out_rows[:limit]


async def _search_notes(
    conn: Any,
    *,
    fts: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """FTS5 over ``screenshot_notes`` via the route-layer helper.

    When ``fts`` is empty we fall back to "most recently updated" — the
    structured query is then acting as a discovery endpoint, not search.
    """
    if fts:
        rows = await _run_notes_fts_search(conn, fts)
        results = [
            {
                "id": int(row["id"]),
                "snippet": row["snippet"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    else:
        where: list[str] = []
        params: list[Any] = []
        if date_from is not None:
            where.append("n.created_at >= ?")
            params.append(_iso(date_from))
        if date_to is not None:
            where.append("n.created_at < ?")
            params.append(_iso(date_to))
        sql = (
            "SELECT n.screenshot_id AS id, "
            "substr(n.body, 1, 200) AS snippet, "
            "n.created_at AS created_at "
            "FROM screenshot_notes n"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY n.updated_at DESC LIMIT ?"
        params.append(limit)
        cursor = await conn.execute(sql, params)
        fetched = await cursor.fetchall()
        results = [
            {
                "id": int(row["id"]),
                "snippet": str(row["snippet"] or ""),
                "created_at": str(row["created_at"]) if row["created_at"] is not None else "",
            }
            for row in fetched
        ]

    # FTS path is already date-agnostic; apply the date window in-Python
    # so callers get consistent semantics across both paths.
    if fts and (date_from is not None or date_to is not None):
        results = [r for r in results if _in_window(r["created_at"], date_from, date_to)]

    return results[:limit]


def _in_window(
    iso_str: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> bool:
    """Return True iff ``iso_str`` is inside ``[date_from, date_to)``."""
    if not iso_str:
        return False
    try:
        when = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if date_from is not None and when < date_from:
        return False
    return not (date_to is not None and when >= date_to)


async def _search_tags(
    conn: Any,
    *,
    fts: str | None,
    tags: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """List tags filtered by literal ``fts`` substring and / or explicit names."""
    where: list[str] = []
    params: list[Any] = []
    if fts:
        where.append("t.name LIKE ?")
        params.append(f"%{fts.strip().lower()}%")
    if tags:
        placeholders = ",".join("?" * len(tags))
        where.append(f"t.name IN ({placeholders})")
        params.extend(tags)

    sql = (
        "SELECT t.id, t.name, t.color, COUNT(st.screenshot_id) AS n "
        "FROM tags t LEFT JOIN screenshot_tags st ON st.tag_id = t.id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY t.id, t.name, t.color ORDER BY n DESC, t.name LIMIT ?"
    params.append(limit)

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "color": row["color"],
            "count": int(row["n"]),
        }
        for row in rows
    ]


async def _search_days(
    conn: Any,
    *,
    app_name: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    tags: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Aggregate screenshot counts per calendar day with the same filter set."""
    where: list[str] = []
    params: list[Any] = []
    if date_from is not None:
        where.append("s.captured_at >= ?")
        params.append(_iso(date_from))
    if date_to is not None:
        where.append("s.captured_at < ?")
        params.append(_iso(date_to))
    if app_name is not None:
        where.append("s.app_name = ?")
        params.append(app_name)

    join_sql = "FROM screenshots s"
    if tags:
        tag_placeholders = ",".join("?" * len(tags))
        join_sql += (
            " JOIN screenshot_tags st ON st.screenshot_id = s.id"
            " JOIN tags t ON t.id = st.tag_id"
        )
        where.append(f"t.name IN ({tag_placeholders})")
        params.extend(tags)

    sql = (
        "SELECT DATE(s.captured_at) AS day, "
        "COUNT(DISTINCT s.id) AS n, "
        "COUNT(DISTINCT s.app_name) AS apps "
        f"{join_sql}"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY day ORDER BY day DESC LIMIT ?"
    params.append(limit)

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "day": str(row["day"]),
            "count": int(row["n"]),
            "apps": int(row["apps"]),
        }
        for row in rows
    ]


async def run_query(query: dict[str, Any]) -> dict[str, Any]:
    """Run a structured query and return mixed results bucketed by kind.

    Parameters
    ----------
    query:
        Dict with keys ``fts``, ``app``, ``date_from``, ``date_to``,
        ``tags`` (list), ``kinds`` (list), ``limit`` (int). Missing keys
        are treated as "no filter" / use defaults. The route layer
        validates this dict via a pydantic v2 model first; in-process
        callers may pass a raw dict and rely on the gentler coercion
        below.

    Returns
    -------
    dict
        ``{"results": {"screenshots": [...], "notes": [...],
        "tags": [...], "days": [...]}, "total_per_kind": {...}}``.
        Kinds the caller did not request are omitted from both maps.
    """
    fts_raw = query.get("fts")
    fts: str | None = fts_raw.strip() if isinstance(fts_raw, str) and fts_raw.strip() else None

    app_raw = query.get("app")
    app_name: str | None = (
        app_raw.strip() if isinstance(app_raw, str) and app_raw.strip() else None
    )

    date_from = _parse_date(query.get("date_from"))
    date_to = _parse_date(query.get("date_to"))
    if date_from is not None and date_to is not None and date_from > date_to:
        msg = "date_from must be <= date_to"
        raise ValueError(msg)

    tags = _normalise_tags(query.get("tags"))
    kinds = _coerce_kinds(query.get("kinds"))
    limit = _coerce_limit(query.get("limit"))

    log.info(
        "query_api.run",
        fts=fts,
        app=app_name,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        tags=tags,
        kinds=kinds,
        limit=limit,
    )

    results: dict[str, list[dict[str, Any]]] = {}
    async with get_connection() as conn:
        if "screenshot" in kinds:
            results["screenshots"] = await _search_screenshots(
                conn,
                fts=fts,
                app_name=app_name,
                date_from=date_from,
                date_to=date_to,
                tags=tags,
                limit=limit,
            )
        if "note" in kinds:
            results["notes"] = await _search_notes(
                conn,
                fts=fts,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        if "tag" in kinds:
            results["tags"] = await _search_tags(
                conn,
                fts=fts,
                tags=tags,
                limit=limit,
            )
        if "day" in kinds:
            results["days"] = await _search_days(
                conn,
                app_name=app_name,
                date_from=date_from,
                date_to=date_to,
                tags=tags,
                limit=limit,
            )

    total_per_kind = {kind: len(rows) for kind, rows in results.items()}
    log.info("query_api.done", total_per_kind=total_per_kind)
    return {"results": results, "total_per_kind": total_per_kind}
