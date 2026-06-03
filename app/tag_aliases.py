"""Tag aliases — collapse multiple spellings onto one canonical tag name.

The tag store (:mod:`app.storage.tags`) is idempotent on ``name`` but
otherwise treats every distinct string as a brand-new tag. Two
spellings of the same concept — ``standup`` and ``daily-standup`` —
therefore accrete as two unrelated rows whose screenshot sets never
merge, splitting one facet across two buckets in the search UI.

This module is the pre-store overlay that fixes that. Every tag-write
path (the LLM auto-tagger in :mod:`app.web.routes.auto_tag` and the
phrase-rule worker pipeline in :func:`app.workers.ocr_worker._apply_phrase_tags`)
funnels its candidate name through :func:`resolve` first, so only the
canonical form is ever passed to :func:`app.storage.tags.create_tag`.
The ``screenshot_tags`` rows therefore never see the alias spelling at
all — search by the canonical name surfaces everything in one bucket.

The async helpers (:func:`set_alias` / :func:`get_canonical` /
:func:`list_all` / :func:`delete`) back the admin UI; :func:`resolve`
is the synchronous fast-path the tagger calls inline. ``resolve``
never raises — a missing alias table (migrations not yet applied) or a
corrupt row degrades silently to identity so the tagging pipeline keeps
working on a fresh checkout.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_aliases")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _normalise(name: Any) -> str:
    """Return the canonical lookup form: trimmed + lowercased.

    All downstream tag code (phrase rules, auto-tag UI, manual tagger)
    already lowercases tag names before storing them, so storing the
    alias key in the same normalised form keeps the equality lookup in
    :func:`resolve` a pure indexed hit with no per-row ``LOWER()`` cost.
    """
    if name is None:
        return ""
    return str(name).strip().lower()


# ---------------------------------------------------------------------------
# Async CRUD helpers (admin UI + tests)
# ---------------------------------------------------------------------------


async def set_alias(alias: str, canonical: str) -> None:
    """Upsert ``alias`` → ``canonical``.

    Both inputs are normalised (trimmed + lowercased). An empty
    ``alias`` is a programming error (raises :class:`ValueError`); an
    empty ``canonical`` is treated as "drop the mapping" and routed
    through :func:`delete` so the row never lingers as a no-op overlay.

    A row that would map an alias to itself is also dropped — a tag is
    always its own canonical, so the explicit row carries no
    information and would just clutter the admin table.
    """
    alias_norm = _normalise(alias)
    canonical_norm = _normalise(canonical)
    if not alias_norm:
        msg = "alias is required"
        raise ValueError(msg)
    if not canonical_norm or canonical_norm == alias_norm:
        await delete(alias_norm)
        return
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO tag_alias (alias, canonical)
            VALUES (?, ?)
            ON CONFLICT(alias) DO UPDATE SET
                canonical = excluded.canonical
            """,
            (alias_norm, canonical_norm),
        )
        await conn.commit()
    log.info("tag_aliases.set", alias=alias_norm, canonical=canonical_norm)


async def get_canonical(name: str) -> str:
    """Return the canonical tag for ``name``, or ``name`` itself if no alias.

    Async counterpart to :func:`resolve` for callers already inside an
    async context (the admin UI, tests). Both helpers share the same
    semantics: a missing alias row, an empty input or any DB error
    collapses to ``name`` unchanged.
    """
    key = _normalise(name)
    if not key:
        return str(name) if name is not None else ""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT canonical FROM tag_alias WHERE alias = ?",
                (key,),
            )
            row = await cursor.fetchone()
    except Exception as exc:
        log.warning("tag_aliases.lookup_failed", name=key, error=str(exc))
        return key
    if row is None:
        return key
    return str(row["canonical"])


async def list_all() -> list[dict[str, str]]:
    """Return every alias row, ordered by alias ASC.

    Each item exposes ``alias``, ``canonical`` and ``created_at`` — the
    admin UI renders one row per item so the operator can see and edit
    every override.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT alias, canonical, created_at FROM tag_alias "
            "ORDER BY alias ASC"
        )
        rows = await cursor.fetchall()
    return [
        {
            "alias": str(row["alias"]),
            "canonical": str(row["canonical"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def delete(alias: str) -> None:
    """Drop the alias row for ``alias``. Idempotent — missing rows are fine."""
    key = _normalise(alias)
    if not key:
        return
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM tag_alias WHERE alias = ?",
            (key,),
        )
        await conn.commit()
    log.info("tag_aliases.deleted", alias=key)


# ---------------------------------------------------------------------------
# Sync resolver (tagger fast-path)
# ---------------------------------------------------------------------------


async def resolve(name: str) -> str:
    """Return the canonical tag name for ``name``.

    This is the helper every tag-write path calls before handing a name
    off to :func:`app.storage.tags.create_tag`. If an alias row exists,
    the canonical form is returned; otherwise the input is returned
    unchanged (normalised to trimmed + lowercased to match the rest of
    the tag pipeline). Never raises — a DB error or missing migration
    degrades to identity so the tagging pipeline keeps working on a
    fresh checkout where ``tag_alias`` does not yet exist.
    """
    return await get_canonical(name)


__all__ = [
    "delete",
    "get_canonical",
    "list_all",
    "resolve",
    "set_alias",
]
