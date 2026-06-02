"""CRUD helpers for the tags + saved_searches tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

from app.storage.time import parse_iso


async def create_tag(
    conn: aiosqlite.Connection,
    *,
    name: str,
    color: str | None = None,
) -> int:
    """Create a new tag, return id. Idempotent on name."""
    await conn.execute(
        "INSERT OR IGNORE INTO tags (name, color) VALUES (?, ?)",
        (name.strip(), color),
    )
    await conn.commit()
    cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (name.strip(),))
    row = await cursor.fetchone()
    if row is None:
        msg = "Tag creation failed unexpectedly"
        raise RuntimeError(msg)
    return int(row["id"])


async def list_tags(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """All tags with their screenshot counts."""
    cursor = await conn.execute(
        "SELECT t.id, t.name, t.color, COUNT(st.screenshot_id) AS n "
        "FROM tags t LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
        "GROUP BY t.id, t.name, t.color ORDER BY n DESC, t.name"
    )
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


async def tag_screenshot(conn: aiosqlite.Connection, screenshot_id: int, tag_id: int) -> None:
    """Attach a tag to a screenshot."""
    await conn.execute(
        "INSERT OR IGNORE INTO screenshot_tags (screenshot_id, tag_id) VALUES (?, ?)",
        (screenshot_id, tag_id),
    )
    await conn.commit()


async def untag_screenshot(conn: aiosqlite.Connection, screenshot_id: int, tag_id: int) -> None:
    await conn.execute(
        "DELETE FROM screenshot_tags WHERE screenshot_id = ? AND tag_id = ?",
        (screenshot_id, tag_id),
    )
    await conn.commit()


async def get_tags_for_many(
    conn: aiosqlite.Connection,
    screenshot_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    """Bulk-fetch tags for a list of screenshot ids → {sid: [{id,name,color}, ...]}."""
    if not screenshot_ids:
        return {}
    placeholders = ",".join("?" * len(screenshot_ids))
    cursor = await conn.execute(
        f"SELECT st.screenshot_id, t.id, t.name, t.color "
        f"FROM screenshot_tags st JOIN tags t ON t.id = st.tag_id "
        f"WHERE st.screenshot_id IN ({placeholders})",
        screenshot_ids,
    )
    rows = await cursor.fetchall()
    out: dict[int, list[dict[str, Any]]] = {sid: [] for sid in screenshot_ids}
    for row in rows:
        sid = int(row["screenshot_id"])
        out.setdefault(sid, []).append(
            {"id": int(row["id"]), "name": str(row["name"]), "color": row["color"]}
        )
    return out


async def get_screenshot_tags(
    conn: aiosqlite.Connection,
    screenshot_id: int,
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT t.id, t.name, t.color FROM tags t "
        "JOIN screenshot_tags st ON st.tag_id = t.id "
        "WHERE st.screenshot_id = ? ORDER BY t.name",
        (screenshot_id,),
    )
    rows = await cursor.fetchall()
    return [
        {"id": int(row["id"]), "name": str(row["name"]), "color": row["color"]}
        for row in rows
    ]


async def list_screenshots_by_tag(
    conn: aiosqlite.Connection,
    tag_id: int,
    *,
    limit: int = 200,
) -> list[int]:
    """Return screenshot ids tagged with the given tag id."""
    cursor = await conn.execute(
        "SELECT screenshot_id FROM screenshot_tags WHERE tag_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        (tag_id, limit),
    )
    rows = await cursor.fetchall()
    return [int(row["screenshot_id"]) for row in rows]


async def save_search(
    conn: aiosqlite.Connection,
    *,
    name: str,
    query: str,
    app_name: str | None = None,
) -> int:
    """Persist a search query as a saved search."""
    cursor = await conn.execute(
        "INSERT INTO saved_searches (name, query, app_name) VALUES (?, ?, ?)",
        (name.strip(), query, app_name),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "Saved search insert failed"
        raise RuntimeError(msg)
    return row_id


async def list_saved_searches(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT id, name, query, app_name, created_at FROM saved_searches ORDER BY created_at DESC"
    )
    rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "query": str(row["query"]),
                "app_name": row["app_name"],
                "created_at": parse_iso(str(row["created_at"])),
            }
        )
    return out


async def delete_saved_search(conn: aiosqlite.Connection, search_id: int) -> None:
    await conn.execute("DELETE FROM saved_searches WHERE id = ?", (search_id,))
    await conn.commit()


async def set_tag_color(
    conn: aiosqlite.Connection,
    tag_id: int,
    *,
    color: str | None,
) -> None:
    cleaned = (color or "").strip()
    if cleaned and not _is_valid_hex(cleaned):
        msg = "Color must be a hex like #a78bfa"
        raise ValueError(msg)
    await conn.execute("UPDATE tags SET color = ? WHERE id = ?", (cleaned or None, tag_id))
    await conn.commit()


async def set_tag_color_by_name(
    conn: aiosqlite.Connection,
    name: str,
    *,
    color: str | None,
) -> int | None:
    """Set ``color`` for the tag identified by ``name``.

    Returns the matched tag id, or ``None`` when no tag with the given
    name exists. Validates the hex value (same rules as
    :func:`set_tag_color`) and writes ``NULL`` when the caller passes
    an empty / falsy colour to mean "clear".
    """
    cleaned_name = name.strip().lower()
    if not cleaned_name:
        msg = "Empty tag name"
        raise ValueError(msg)
    cleaned_color = (color or "").strip()
    if cleaned_color and not _is_valid_hex(cleaned_color):
        msg = "Color must be a hex like #a78bfa"
        raise ValueError(msg)
    cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (cleaned_name,))
    row = await cursor.fetchone()
    if row is None:
        return None
    tag_id = int(row["id"])
    await conn.execute(
        "UPDATE tags SET color = ? WHERE id = ?",
        (cleaned_color or None, tag_id),
    )
    await conn.commit()
    return tag_id


def _is_valid_hex(value: str) -> bool:
    if not value.startswith("#"):
        return False
    body = value[1:]
    if len(body) not in (3, 6, 8):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in body)


async def saved_search_new_count(
    conn: aiosqlite.Connection,
    *,
    search_id: int,
    fts_query_callback,
) -> int:
    """Compute "new since last seen" for a saved search via a callback that runs
    the FTS search (we don't want a hard import cycle into app.search here).
    """
    cursor = await conn.execute(
        "SELECT query, app_name, last_seen_screenshot_id FROM saved_searches WHERE id = ?",
        (search_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    hits = await fts_query_callback(
        conn,
        query=str(row["query"]),
        limit=200,
        app_name=row["app_name"],
    )
    last_seen = int(row["last_seen_screenshot_id"])
    return sum(1 for h in hits if h.screenshot_id > last_seen)


async def saved_search_mark_seen(
    conn: aiosqlite.Connection,
    *,
    search_id: int,
    highest_id: int,
) -> None:
    await conn.execute(
        "UPDATE saved_searches SET last_seen_screenshot_id = ?, last_seen_at = datetime('now') "
        "WHERE id = ?",
        (highest_id, search_id),
    )
    await conn.commit()


async def rename_tag(
    conn: aiosqlite.Connection,
    tag_id: int,
    *,
    new_name: str,
) -> None:
    """Rename a tag in-place. Idempotent if `new_name` already exists — silently merges."""
    new_name = new_name.strip().lower()
    if not new_name:
        msg = "Empty tag name"
        raise ValueError(msg)

    cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (new_name,))
    row = await cursor.fetchone()
    if row is not None and int(row["id"]) != tag_id:
        await merge_tag(conn, source_id=tag_id, target_id=int(row["id"]))
        return

    await conn.execute("UPDATE tags SET name = ? WHERE id = ?", (new_name, tag_id))
    await conn.commit()


async def merge_tag(
    conn: aiosqlite.Connection,
    *,
    source_id: int,
    target_id: int,
) -> int:
    """Move every screenshot from source tag to target tag, delete source. Returns count moved."""
    if source_id == target_id:
        return 0
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO screenshot_tags (screenshot_id, tag_id) "
        "SELECT screenshot_id, ? FROM screenshot_tags WHERE tag_id = ?",
        (target_id, source_id),
    )
    moved = cursor.rowcount or 0
    await conn.execute("DELETE FROM screenshot_tags WHERE tag_id = ?", (source_id,))
    await conn.execute("DELETE FROM tags WHERE id = ?", (source_id,))
    await conn.commit()
    return moved


async def delete_tag(conn: aiosqlite.Connection, tag_id: int) -> None:
    """Delete a tag and all its screenshot-bindings."""
    await conn.execute("DELETE FROM screenshot_tags WHERE tag_id = ?", (tag_id,))
    await conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    await conn.commit()


async def co_tag_counts(
    conn: aiosqlite.Connection,
    tag_id: int,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """For a given tag, list other tags that frequently co-occur on the same screenshots."""
    cursor = await conn.execute(
        """
        SELECT t2.id, t2.name, COUNT(*) AS n
        FROM screenshot_tags st1
        JOIN screenshot_tags st2 ON st2.screenshot_id = st1.screenshot_id AND st2.tag_id != st1.tag_id
        JOIN tags t2 ON t2.id = st2.tag_id
        WHERE st1.tag_id = ?
        GROUP BY t2.id, t2.name
        ORDER BY n DESC
        LIMIT ?
        """,
        (tag_id, limit),
    )
    rows = await cursor.fetchall()
    return [
        {"id": int(row["id"]), "name": str(row["name"]), "count": int(row["n"])}
        for row in rows
    ]


async def per_day_for_tag(
    conn: aiosqlite.Connection,
    tag_id: int,
    *,
    days: int = 60,
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        """
        SELECT DATE(s.captured_at) AS day, COUNT(*) AS n
        FROM screenshot_tags st
        JOIN screenshots s ON s.id = st.screenshot_id
        WHERE st.tag_id = ? AND s.captured_at >= DATE('now', ?)
        GROUP BY day ORDER BY day
        """,
        (tag_id, f"-{days} days"),
    )
    rows = await cursor.fetchall()
    return [{"day": str(row["day"]), "count": int(row["n"])} for row in rows]


def _from_isoformat(value: str) -> datetime:
    return datetime.fromisoformat(value)
