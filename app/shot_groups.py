"""Explicit, hand-curated screenshot groups (a.k.a. "cherry-pick bundles").

Persona already exposes :mod:`app.web.routes.auto_collections` for
*tag-driven* membership and :mod:`app.query_collections` for *query-driven*
membership. Both compute their contents on read, which is great for "show
me everything tagged ``invoice``" but unhelpful when the operator wants
to gather a specific, frozen-in-time set of shots — e.g. "the seven
slides I'm e-mailing to the customer on Tuesday".

This module owns the missing primitive: a named bundle whose membership
only changes when an explicit add/remove happens. The schema (see
:file:`storage/migrations/080_shot_groups.sql`) consists of two tables:

* ``shot_group``        — one row per named bundle (slug + title).
* ``shot_group_member`` — many-to-many between ``shot_group`` and
  ``screenshots`` keyed on the composite ``(group_slug, shot_id)``.

The helpers below are deliberately small and side-effect-free except
for the obvious storage write. Every SQL statement uses bound parameters
— no string interpolation reaches the cursor — so user-supplied slugs
and shot ids cannot smuggle SQL into the query.

Naming convention: a slug is validated by the calling route against
``^[a-z0-9-]{1,40}$`` so it survives a URL path segment without
percent-encoding. The helpers re-strip and lower-case here as a
defence-in-depth nudge but do not re-run the regex — callers must.
"""

from __future__ import annotations

from typing import TypedDict

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shot_groups")


class ShotGroup(TypedDict):
    """One row of :func:`list_groups` output."""

    slug: str
    title: str
    created_at: str
    member_count: int


class ShotGroupMember(TypedDict):
    """One row of :func:`members_of` output (id-only, route hydrates shots)."""

    shot_id: int
    added_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def create_group(slug: str, title: str) -> None:
    """Insert a new ``(slug, title)`` row.

    Raises :class:`aiosqlite.IntegrityError` when ``slug`` already
    exists; the caller (web route) translates that into a 409 / 400 so
    the human-facing error is shaped by the layer that owns the form.

    Both inputs are stripped; an empty value after stripping raises
    :class:`ValueError` so the storage layer never persists a group with
    a blank title or slug — those would render as ghost rows on the
    index page.
    """
    slug_clean = slug.strip().lower()
    title_clean = title.strip()
    if not slug_clean:
        msg = "slug is required"
        raise ValueError(msg)
    if not title_clean:
        msg = "title is required"
        raise ValueError(msg)
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO shot_group (slug, title) VALUES (?, ?)",
            (slug_clean, title_clean),
        )
        await conn.commit()
    log.info("shot_groups.created", slug=slug_clean, title=title_clean)


async def add_member(slug: str, shot_id: int) -> bool:
    """Add ``shot_id`` to ``slug``. Idempotent — re-adds are silent no-ops.

    Returns ``True`` when a new row was inserted, ``False`` when the
    member already existed. The route uses the boolean to choose between
    "added" and "already there" log lines; callers that don't care can
    ignore the return value.
    """
    slug_clean = slug.strip().lower()
    if not slug_clean:
        msg = "slug is required"
        raise ValueError(msg)
    if shot_id <= 0:
        msg = "shot_id must be a positive integer"
        raise ValueError(msg)
    async with get_connection() as conn:
        # Pre-check that the group exists so a stray POST never silently
        # creates an orphan row in ``shot_group_member`` (the FK-less
        # schema would otherwise let it slide).
        cursor = await conn.execute(
            "SELECT 1 FROM shot_group WHERE slug = ?",
            (slug_clean,),
        )
        if await cursor.fetchone() is None:
            log.warning("shot_groups.add.missing_group", slug=slug_clean, shot_id=shot_id)
            msg = f"group {slug_clean!r} does not exist"
            raise LookupError(msg)
        try:
            await conn.execute(
                "INSERT INTO shot_group_member (group_slug, shot_id) VALUES (?, ?)",
                (slug_clean, shot_id),
            )
            await conn.commit()
        except aiosqlite.IntegrityError:
            # Composite PK conflict → membership row already exists.
            log.debug("shot_groups.add.duplicate", slug=slug_clean, shot_id=shot_id)
            return False
    log.info("shot_groups.add", slug=slug_clean, shot_id=shot_id)
    return True


async def remove_member(slug: str, shot_id: int) -> bool:
    """Remove ``shot_id`` from ``slug``. Idempotent.

    Returns ``True`` when a row was deleted, ``False`` when nothing
    matched. The boolean lets the route distinguish "you just removed
    it" from "it wasn't there" for telemetry without a second roundtrip.
    """
    slug_clean = slug.strip().lower()
    if not slug_clean:
        msg = "slug is required"
        raise ValueError(msg)
    if shot_id <= 0:
        msg = "shot_id must be a positive integer"
        raise ValueError(msg)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM shot_group_member WHERE group_slug = ? AND shot_id = ?",
            (slug_clean, shot_id),
        )
        await conn.commit()
        removed = cursor.rowcount > 0
    if removed:
        log.info("shot_groups.remove", slug=slug_clean, shot_id=shot_id)
    else:
        log.debug("shot_groups.remove.missing", slug=slug_clean, shot_id=shot_id)
    return removed


async def list_groups() -> list[ShotGroup]:
    """Return every group with its current member count, newest first.

    The member count is computed with a ``LEFT JOIN`` so groups with
    zero members appear in the list (they would otherwise be impossible
    to delete from the UI once emptied).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT g.slug AS slug, g.title AS title, g.created_at AS created_at, "
            "       COUNT(m.shot_id) AS member_count "
            "FROM shot_group g "
            "LEFT JOIN shot_group_member m ON m.group_slug = g.slug "
            "GROUP BY g.slug, g.title, g.created_at "
            "ORDER BY g.created_at DESC, g.slug ASC",
        )
        rows = await cursor.fetchall()
    return [
        ShotGroup(
            slug=str(row["slug"]),
            title=str(row["title"]),
            created_at=str(row["created_at"]),
            member_count=int(row["member_count"]),
        )
        for row in rows
    ]


async def get_group(slug: str) -> ShotGroup | None:
    """Fetch one group by slug (member count included), or ``None``."""
    slug_clean = slug.strip().lower()
    if not slug_clean:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT g.slug AS slug, g.title AS title, g.created_at AS created_at, "
            "       COUNT(m.shot_id) AS member_count "
            "FROM shot_group g "
            "LEFT JOIN shot_group_member m ON m.group_slug = g.slug "
            "WHERE g.slug = ? "
            "GROUP BY g.slug, g.title, g.created_at",
            (slug_clean,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return ShotGroup(
        slug=str(row["slug"]),
        title=str(row["title"]),
        created_at=str(row["created_at"]),
        member_count=int(row["member_count"]),
    )


async def members_of(slug: str, *, limit: int = 500) -> list[ShotGroupMember]:
    """Return the ``(shot_id, added_at)`` pairs for ``slug``, newest first.

    ``limit`` defaults to 500 — same ceiling as
    :data:`app.web.routes.auto_collections._MAX_SHOTS_PER_COLLECTION` so
    the two grid pages have a consistent memory footprint. The route
    layer hydrates each ``shot_id`` via
    :func:`app.storage.repository.get_screenshot`; missing shots
    (deleted but membership row still around) are simply skipped at
    render time.
    """
    slug_clean = slug.strip().lower()
    if not slug_clean:
        return []
    if limit <= 0:
        return []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT shot_id, added_at FROM shot_group_member "
            "WHERE group_slug = ? "
            "ORDER BY added_at DESC, shot_id DESC "
            "LIMIT ?",
            (slug_clean, limit),
        )
        rows = await cursor.fetchall()
    return [
        ShotGroupMember(
            shot_id=int(row["shot_id"]),
            added_at=str(row["added_at"]),
        )
        for row in rows
    ]


async def delete_group(slug: str) -> bool:
    """Drop the group and every membership row in one transaction.

    Returns ``True`` when the group existed (and was removed),
    ``False`` when the slug had no row. The two-statement transaction
    is wrapped in a single ``commit`` so a crash between the member
    delete and the group delete cannot leave orphan rows pointing at a
    vanished slug.
    """
    slug_clean = slug.strip().lower()
    if not slug_clean:
        msg = "slug is required"
        raise ValueError(msg)
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM shot_group_member WHERE group_slug = ?",
            (slug_clean,),
        )
        cursor = await conn.execute(
            "DELETE FROM shot_group WHERE slug = ?",
            (slug_clean,),
        )
        await conn.commit()
        removed = cursor.rowcount > 0
    if removed:
        log.info("shot_groups.deleted", slug=slug_clean)
    else:
        log.debug("shot_groups.delete.missing", slug=slug_clean)
    return removed


__all__ = [
    "ShotGroup",
    "ShotGroupMember",
    "add_member",
    "create_group",
    "delete_group",
    "get_group",
    "list_groups",
    "members_of",
    "remove_member",
]
