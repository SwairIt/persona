"""Saved-query collections — bundle saved searches under one public slug.

Backed by migration ``059_query_collections.sql``. Two tables:

* ``query_collection`` — one row per collection (``slug``, ``title``, optional
  ``blurb``).
* ``query_collection_member`` — many rows per collection; each links a
  ``saved_search`` row by slug and carries an explicit display ``position``.

Helpers in this module own all parametrised SQL the route layer needs;
the route layer is responsible only for HTTP shape (forms, redirects,
templates). Every public function validates the inputs that flow into
SQL via a strict slug regex.
"""

from __future__ import annotations

import re
from typing import Any

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.query_collections")

SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
TITLE_MIN, TITLE_MAX = 1, 100
BLURB_MAX = 500


class QueryCollectionError(ValueError):
    """Raised on validation failure or a duplicate slug.

    Sub-classing :class:`ValueError` keeps callers that only want
    "bad input" handling simple while still letting the route layer
    map us onto an HTTP 400.
    """


def _validate_slug(slug: str) -> str:
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        msg = "slug must match ^[a-z0-9-]{1,40}$"
        raise QueryCollectionError(msg)
    return cleaned


def _validate_title(title: str) -> str:
    cleaned = (title or "").strip()
    if not (TITLE_MIN <= len(cleaned) <= TITLE_MAX):
        msg = f"title must be {TITLE_MIN}..{TITLE_MAX} characters"
        raise QueryCollectionError(msg)
    return cleaned


def _validate_blurb(blurb: str | None) -> str | None:
    if blurb is None:
        return None
    cleaned = blurb.strip()
    if not cleaned:
        return None
    if len(cleaned) > BLURB_MAX:
        msg = f"blurb must be at most {BLURB_MAX} characters"
        raise QueryCollectionError(msg)
    return cleaned


def _row_to_collection(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "blurb": None if row["blurb"] is None else str(row["blurb"]),
        "created_at": str(row["created_at"]),
    }


async def create(
    slug: str,
    title: str,
    blurb: str | None = None,
) -> dict[str, Any]:
    """Insert a new collection. Raises :class:`QueryCollectionError` on dupes."""
    slug_v = _validate_slug(slug)
    title_v = _validate_title(title)
    blurb_v = _validate_blurb(blurb)

    async with get_connection() as conn:
        try:
            await conn.execute(
                "INSERT INTO query_collection (slug, title, blurb) "
                "VALUES (?, ?, ?)",
                (slug_v, title_v, blurb_v),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            log.warning("query_collections.duplicate", slug=slug_v)
            msg = f"collection {slug_v!r} already exists"
            raise QueryCollectionError(msg) from exc

        cursor = await conn.execute(
            "SELECT slug, title, blurb, created_at "
            "FROM query_collection WHERE slug = ?",
            (slug_v,),
        )
        row = await cursor.fetchone()

    if row is None:  # pragma: no cover — INSERT just succeeded.
        msg = f"collection {slug_v!r} vanished right after insert"
        raise QueryCollectionError(msg)

    log.info("query_collections.created", slug=slug_v, title=title_v)
    return _row_to_collection(row)


async def add_query(
    collection_slug: str,
    search_slug: str,
    position: int,
) -> None:
    """Attach ``search_slug`` to ``collection_slug`` at the given ``position``.

    Both slugs must already exist; ``position`` is a non-negative int used
    purely for ordering on the public page. Re-adding the same pair raises
    :class:`QueryCollectionError` rather than silently overwriting.
    """
    collection_v = _validate_slug(collection_slug)
    search_v = _validate_slug(search_slug)
    if not isinstance(position, int) or position < 0:
        msg = "position must be a non-negative integer"
        raise QueryCollectionError(msg)

    async with get_connection() as conn:
        coll_cur = await conn.execute(
            "SELECT 1 FROM query_collection WHERE slug = ?",
            (collection_v,),
        )
        if await coll_cur.fetchone() is None:
            msg = f"collection {collection_v!r} not found"
            raise QueryCollectionError(msg)

        search_cur = await conn.execute(
            "SELECT 1 FROM saved_search WHERE slug = ?",
            (search_v,),
        )
        if await search_cur.fetchone() is None:
            msg = f"saved search {search_v!r} not found"
            raise QueryCollectionError(msg)

        try:
            await conn.execute(
                "INSERT INTO query_collection_member "
                "(collection_slug, saved_search_slug, position) "
                "VALUES (?, ?, ?)",
                (collection_v, search_v, position),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            log.warning(
                "query_collections.member_duplicate",
                collection=collection_v,
                search=search_v,
            )
            msg = (
                f"saved search {search_v!r} already in collection "
                f"{collection_v!r}"
            )
            raise QueryCollectionError(msg) from exc

    log.info(
        "query_collections.member_added",
        collection=collection_v,
        search=search_v,
        position=position,
    )


async def get(slug: str) -> dict[str, Any] | None:
    """Return one collection plus its ordered members, or ``None`` if missing.

    Members are joined against ``saved_search`` so the caller can render
    each query without a second round-trip.
    """
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        coll_cur = await conn.execute(
            "SELECT slug, title, blurb, created_at "
            "FROM query_collection WHERE slug = ?",
            (slug_v,),
        )
        coll_row = await coll_cur.fetchone()
        if coll_row is None:
            return None

        member_cur = await conn.execute(
            "SELECT m.saved_search_slug AS slug, "
            "       m.position          AS position, "
            "       s.title             AS title, "
            "       s.query             AS query "
            "FROM query_collection_member AS m "
            "JOIN saved_search           AS s "
            "  ON s.slug = m.saved_search_slug "
            "WHERE m.collection_slug = ? "
            "ORDER BY m.position ASC, s.title COLLATE NOCASE ASC",
            (slug_v,),
        )
        member_rows = await member_cur.fetchall()

    collection = _row_to_collection(coll_row)
    collection["members"] = [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "query": str(row["query"]),
            "position": int(row["position"]),
        }
        for row in member_rows
    ]
    return collection


async def list_all() -> list[dict[str, Any]]:
    """Return every collection, newest first, with a precomputed member count."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT c.slug        AS slug, "
            "       c.title       AS title, "
            "       c.blurb       AS blurb, "
            "       c.created_at  AS created_at, "
            "       COUNT(m.saved_search_slug) AS member_count "
            "FROM query_collection AS c "
            "LEFT JOIN query_collection_member AS m "
            "  ON m.collection_slug = c.slug "
            "GROUP BY c.slug, c.title, c.blurb, c.created_at "
            "ORDER BY c.created_at DESC",
        )
        rows = await cursor.fetchall()

    return [
        {
            **_row_to_collection(row),
            "member_count": int(row["member_count"]),
        }
        for row in rows
    ]


async def delete(slug: str) -> None:
    """Remove a collection and all its memberships. No-op if missing."""
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM query_collection_member WHERE collection_slug = ?",
            (slug_v,),
        )
        await conn.execute(
            "DELETE FROM query_collection WHERE slug = ?",
            (slug_v,),
        )
        await conn.commit()
    log.info("query_collections.deleted", slug=slug_v)
