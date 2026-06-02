"""Reusable note templates — pre-baked bodies a user can paste into a new note.

Each template has a short URL-safe ``slug`` (primary key), a human ``title``,
and a markdown ``body``. The web UI lists them; an "apply" endpoint returns
the raw body so the frontend can paste it into a textarea.
"""

from __future__ import annotations

import re
from typing import Any

import aiosqlite

from app.logging_setup import get_logger

log = get_logger("persona.note_templates")

SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def _validate_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        msg = "slug must match ^[a-z0-9-]{1,40}$"
        raise ValueError(msg)
    return slug


async def list_templates(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return every template, ordered by title."""
    cursor = await conn.execute(
        "SELECT slug, title, body, created_at "
        "FROM note_template ORDER BY title COLLATE NOCASE"
    )
    rows = await cursor.fetchall()
    return [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "body": str(row["body"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def get_template(
    conn: aiosqlite.Connection,
    slug: str,
) -> dict[str, Any] | None:
    """Return a single template by slug, or None if missing."""
    slug = _validate_slug(slug)
    cursor = await conn.execute(
        "SELECT slug, title, body, created_at FROM note_template WHERE slug = ?",
        (slug,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "body": str(row["body"]),
        "created_at": str(row["created_at"]),
    }


async def create_template(
    conn: aiosqlite.Connection,
    *,
    slug: str,
    title: str,
    body: str,
) -> str:
    """Insert a new template; raises ValueError on bad slug or duplicate."""
    slug = _validate_slug(slug)
    title = (title or "").strip()
    body = body or ""
    if not title:
        msg = "title is required"
        raise ValueError(msg)
    if not body.strip():
        msg = "body is required"
        raise ValueError(msg)

    try:
        await conn.execute(
            "INSERT INTO note_template (slug, title, body) VALUES (?, ?, ?)",
            (slug, title, body),
        )
        await conn.commit()
    except aiosqlite.IntegrityError as exc:
        msg = f"slug {slug!r} already exists"
        raise ValueError(msg) from exc

    log.info("note_templates.created", slug=slug, title=title)
    return slug


async def delete_template(conn: aiosqlite.Connection, slug: str) -> None:
    """Delete a template by slug. No-op if it doesn't exist."""
    slug = _validate_slug(slug)
    await conn.execute("DELETE FROM note_template WHERE slug = ?", (slug,))
    await conn.commit()
    log.info("note_templates.deleted", slug=slug)
