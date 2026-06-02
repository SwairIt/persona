"""Public-day opt-in: admin marks specific days as public.

A "public day" is a row in :mod:`app.storage.migrations.043_public_days`
that maps a local ``YYYY-MM-DD`` calendar day to an externally visible
``slug`` plus presentation metadata (``title``, ``blurb``). The HTTP
layer in :mod:`app.web.routes.public_day` reads these rows to render a
stripped-down, unauthenticated view of that day at
``/public/day/{slug}``.

This module owns the data plane only — slug validation, parametrised
SQL, and the four CRUD-ish helpers the route module needs. Filtering
sensitive content (private tags, redacted OCR, hidden annotations) is
done by the route at render time, not here, so toggling a tag or rule
takes effect immediately without touching any of these rows.
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.public_day")

# Slug grammar: lowercase letters, digits, hyphens, 1-60 chars.
# The same regex is mirrored in the admin template's HTML5 ``pattern``
# attribute so the browser hints before a round-trip.
_SLUG_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9-]{1,60}$")

# YYYY-MM-DD shape — anything else is rejected with a ValueError so a
# typo never lands a permanent row in the table.
_DAY_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_slug(slug: str) -> str:
    """Return ``slug`` if it matches ``^[a-z0-9-]{1,60}$``, else raise."""
    cleaned = (slug or "").strip()
    if not _SLUG_RE.fullmatch(cleaned):
        msg = "slug must be 1-60 chars of lowercase letters, digits, or hyphens"
        raise ValueError(msg)
    return cleaned


def _validate_day(day: str) -> str:
    """Return ``day`` if it looks like ``YYYY-MM-DD``, else raise."""
    cleaned = (day or "").strip()
    if not _DAY_RE.fullmatch(cleaned):
        msg = "day must be YYYY-MM-DD"
        raise ValueError(msg)
    return cleaned


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "day": str(row["day"]),
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "blurb": (str(row["blurb"]) if row["blurb"] is not None else None),
        "published_at": str(row["published_at"]),
    }


async def publish(
    day: str,
    slug: str,
    title: str,
    blurb: str | None = None,
) -> None:
    """Mark ``day`` as public under ``slug`` with ``title`` and ``blurb``.

    Re-publishing the same day overwrites the slug/title/blurb so the
    admin can rename without first un-publishing. The DB-level
    ``UNIQUE`` constraint on ``slug`` still guards against two different
    days claiming the same URL — a ``sqlite3.IntegrityError`` surfaces
    in that case and the caller renders a 400.
    """
    day_clean = _validate_day(day)
    slug_clean = _validate_slug(slug)
    title_clean = (title or "").strip()
    if not title_clean:
        msg = "title is required"
        raise ValueError(msg)
    blurb_clean: str | None = None
    if blurb is not None:
        stripped = blurb.strip()
        blurb_clean = stripped or None

    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO public_day (day, slug, title, blurb)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                slug = excluded.slug,
                title = excluded.title,
                blurb = excluded.blurb,
                published_at = datetime('now')
            """,
            (day_clean, slug_clean, title_clean, blurb_clean),
        )
        await conn.commit()
    log.info(
        "public_day.publish",
        day=day_clean,
        slug=slug_clean,
        title=title_clean,
    )


async def unpublish(day: str) -> None:
    """Remove the public-day row for ``day``. Idempotent if missing."""
    day_clean = _validate_day(day)
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM public_day WHERE day = ?",
            (day_clean,),
        )
        await conn.commit()
    log.info("public_day.unpublish", day=day_clean)


async def get_by_slug(slug: str) -> dict[str, Any] | None:
    """Look up a published day by its URL slug, or ``None`` if absent.

    Invalid slugs (anything outside the regex) return ``None`` rather
    than raising — the route turns that into a clean 404 instead of a
    500. A malformed slug in the URL is a client problem, not a server
    one.
    """
    try:
        slug_clean = _validate_slug(slug)
    except ValueError:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day, slug, title, blurb, published_at FROM public_day WHERE slug = ?",
            (slug_clean,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def list_published() -> list[dict[str, Any]]:
    """Every published day, newest first by ``published_at``."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day, slug, title, blurb, published_at "
            "FROM public_day "
            "ORDER BY published_at DESC, day DESC"
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


__all__ = [
    "get_by_slug",
    "list_published",
    "publish",
    "unpublish",
]
