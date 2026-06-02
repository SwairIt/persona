"""Shareable permalinks for any Persona page state.

A *permalink* is an opaque 8-character base36 slug that maps to a
relative ``target_url`` already inside the app. The HTTP layer in
:mod:`app.web.routes.permalinks` lets the operator paste the current
browser URL (``location.href`` is trimmed to path + query + hash by
the JS button) into a form and get back a short ``/go/{slug}`` link
that is easier to share over chat than a 200-character URL stuffed
with filters.

This module owns the data plane only — slug minting, parametrised SQL,
and the four helpers the route module needs. The slug grammar is
``[0-9a-z]{8}`` (base36) so it is URL-safe without escaping and
case-insensitive lookups are unnecessary. ``secrets.randbelow`` gives
us a cryptographically random 36**8 ≈ 2.8e12 space, large enough that
a naive insert with a single retry on collision is overwhelmingly
sufficient for hand-curated lists.

Open-redirect guard: ``create`` rejects any ``target_url`` that does
not start with ``/`` — a permalink can only ever redirect *inside*
Persona. This is the only spot where unvetted user input lands in the
``permalink.target_url`` column, so the check lives here next to the
INSERT.
"""

from __future__ import annotations

import secrets
import string
from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.permalinks")

# Slug grammar: lowercase letters + digits, fixed length.
_SLUG_ALPHABET: Final[str] = string.digits + string.ascii_lowercase  # base36, 36 chars
_SLUG_LENGTH: Final[int] = 8
_SLUG_BASE: Final[int] = len(_SLUG_ALPHABET)

# Cap on retries when a freshly minted slug collides with an existing
# row. With 36**8 ≈ 2.8e12 possible slugs the birthday-paradox crossover
# only matters at hundreds of thousands of rows; three retries here is
# astronomically more than enough headroom.
_MAX_MINT_RETRIES: Final[int] = 5

# Hard ceiling on stored ``target_url`` and ``label`` length. SQLite has
# no varchar enforcement so we trim at the boundary — a 4 KiB URL is
# already absurd for a permalink and a 200-char label is comfortable.
_MAX_URL_LEN: Final[int] = 4096
_MAX_LABEL_LEN: Final[int] = 200


def _mint_slug() -> str:
    """Return a fresh 8-char base36 slug via :func:`secrets.randbelow`."""
    chars: list[str] = []
    for _ in range(_SLUG_LENGTH):
        chars.append(_SLUG_ALPHABET[secrets.randbelow(_SLUG_BASE)])
    return "".join(chars)


def _validate_target_url(target_url: str) -> str:
    """Return ``target_url`` if it looks like a relative path, else raise.

    A permalink may only point at another Persona page — anything that
    looks like an absolute URL (scheme, protocol-relative, or a bare
    domain) is rejected so this table cannot be weaponised as an open
    redirect. The check is intentionally strict: the only accepted shape
    is ``/...``.
    """
    cleaned = (target_url or "").strip()
    if not cleaned:
        msg = "target_url is required"
        raise ValueError(msg)
    if len(cleaned) > _MAX_URL_LEN:
        msg = f"target_url must be <= {_MAX_URL_LEN} chars"
        raise ValueError(msg)
    # Reject protocol-relative ``//evil.example`` up front — the leading
    # ``/`` would otherwise sneak past the next check.
    if cleaned.startswith("//"):
        msg = "target_url must be a relative path (no protocol-relative URLs)"
        raise ValueError(msg)
    if not cleaned.startswith("/"):
        msg = "target_url must start with '/' (relative path, no open redirect)"
        raise ValueError(msg)
    return cleaned


def _validate_label(label: str | None) -> str | None:
    """Trim ``label`` and enforce the length cap; empty -> ``None``."""
    if label is None:
        return None
    cleaned = label.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_LABEL_LEN:
        cleaned = cleaned[:_MAX_LABEL_LEN]
    return cleaned


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "slug": str(row["slug"]),
        "target_url": str(row["target_url"]),
        "label": (str(row["label"]) if row["label"] is not None else None),
        "created_at": str(row["created_at"]),
        "hits": int(row["hits"]),
    }


async def create(target_url: str, label: str | None = None) -> str:
    """Insert a new permalink row and return its slug.

    The slug is minted client-side (in Python) via
    :func:`secrets.randbelow`; on the off-chance of a collision with an
    existing row the helper re-mints up to :data:`_MAX_MINT_RETRIES`
    times before surfacing the IntegrityError as a ``RuntimeError`` so
    the route layer turns it into a 500 (rather than silently looping).
    """
    target_clean = _validate_target_url(target_url)
    label_clean = _validate_label(label)

    async with get_connection() as conn:
        for attempt in range(_MAX_MINT_RETRIES):
            slug = _mint_slug()
            cursor = await conn.execute(
                "SELECT 1 FROM permalink WHERE slug = ?",
                (slug,),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                continue
            await conn.execute(
                "INSERT INTO permalink (slug, target_url, label) VALUES (?, ?, ?)",
                (slug, target_clean, label_clean),
            )
            await conn.commit()
            log.info(
                "permalinks.create",
                slug=slug,
                target_url=target_clean,
                label=label_clean,
                attempt=attempt,
            )
            return slug

    msg = "could not mint a unique permalink slug after retries"
    raise RuntimeError(msg)


async def get(slug: str) -> dict[str, Any] | None:
    """Look up a permalink by slug, or ``None`` if absent.

    Malformed slugs (anything outside the fixed 8-char base36 grammar)
    return ``None`` so the redirect route emits a clean 404 instead of
    a 500. A bad slug in the URL is a client problem.
    """
    cleaned = (slug or "").strip().lower()
    if len(cleaned) != _SLUG_LENGTH:
        return None
    if any(ch not in _SLUG_ALPHABET for ch in cleaned):
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, target_url, label, created_at, hits "
            "FROM permalink WHERE slug = ?",
            (cleaned,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def bump_hits(slug: str) -> None:
    """Atomically increment ``hits`` for ``slug``. No-op if missing.

    Same slug-grammar guard as :func:`get`: a malformed slug never
    touches the DB, since the redirect route would already have 404'd
    before reaching this call in practice.
    """
    cleaned = (slug or "").strip().lower()
    if len(cleaned) != _SLUG_LENGTH:
        return
    if any(ch not in _SLUG_ALPHABET for ch in cleaned):
        return
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE permalink SET hits = hits + 1 WHERE slug = ?",
            (cleaned,),
        )
        await conn.commit()


async def list_recent(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recently created permalinks, newest first."""
    capped = max(1, min(int(limit), 500))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, target_url, label, created_at, hits "
            "FROM permalink "
            "ORDER BY created_at DESC, slug DESC "
            "LIMIT ?",
            (capped,),
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


__all__ = [
    "bump_hits",
    "create",
    "get",
    "list_recent",
]
