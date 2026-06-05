"""Freehand sketch notes — create / list / fetch / delete / rename.

A sketch is a hand-drawn doodle the user captures at ``/sketch``: a
stylus or mouse drag yields one or more SVG ``<path>`` elements which
the editor stitches into a complete ``<svg ...>...</svg>`` document.
The payload is stored verbatim in ``sketch_note.svg_payload`` (see
migration ``135_sketch_note.sql``) and rendered by handing the bytes
back to the browser with ``Content-Type: image/svg+xml``.

Design notes:

* **Sanitisation is mandatory.** The editor talks to ``POST
  /api/sketches`` over JSON, so a hostile or buggy client could ship
  arbitrary markup. :func:`sanitize_svg` strips ``<script>``
  elements, ``on*=`` event handlers, and ``javascript:`` URIs — the
  three vectors that turn an SVG inline into stored XSS the moment we
  render it. Other markup (text labels, fills, gradients) is left
  intact so the editor can grow without touching the helper.
* **All filesystem-shaped IO is async.** Every helper opens its own
  connection via :func:`app.storage.db.get_connection`, mirroring the
  call style used by :mod:`app.shot_privacy_masks` and
  :mod:`app.focus_whitelist`. SQL is fully parametrised; there's no
  string interpolation against user input.
* **No raster fallback.** SVG is the only stored form. Routes that
  want a thumbnail simply render the document inside an ``<svg>``
  viewport — the browser handles scaling. This keeps the module free
  of Pillow / cairo dependencies and avoids a server-side worker.
* **Idempotent deletes.** :func:`delete_sketch` swallows a missing
  row silently; the contract is "after this returns, the row is not
  there", matching the rest of the v1.4x memory surfaces.
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sketch_notes")

# Strip ``<script>...</script>`` blocks (case-insensitive, dotall so
# multi-line scripts are caught). The SVG editor never emits scripts —
# the only way one shows up here is a hostile or buggy client.
_SCRIPT_RE: Final[re.Pattern[str]] = re.compile(
    r"<script\b[^>]*>.*?</script\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Bare ``<script ... />`` self-closing form, which the broad block
# above does not cover.
_SCRIPT_SELF_CLOSE_RE: Final[re.Pattern[str]] = re.compile(
    r"<script\b[^>]*/\s*>",
    re.IGNORECASE,
)

# Inline ``on*=`` event handlers — onclick, onload, onerror, etc.
# Matches both single- and double-quoted values, and the (rare but
# legal) unquoted form.
_ON_ATTR_RE: Final[re.Pattern[str]] = re.compile(
    r"""\s+on[a-zA-Z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)

# ``href`` / ``xlink:href`` / ``src`` values that start with the
# ``javascript:`` pseudo-scheme. We rewrite them to the empty string
# rather than removing the attribute to preserve element structure.
_JS_URI_RE: Final[re.Pattern[str]] = re.compile(
    r"""(href|xlink:href|src)\s*=\s*(["'])\s*javascript:[^"']*\2""",
    re.IGNORECASE,
)


def sanitize_svg(payload: str) -> str:
    """Strip script tags, on-event handlers, and ``javascript:`` URIs.

    The editor never emits any of these, so a clean payload round-trips
    byte-for-byte. A hostile payload is stripped of the three vectors
    that matter for stored-XSS when the SVG is later inlined or served
    as ``image/svg+xml`` and rendered inside an ``<object>`` /
    ``<iframe>``.

    The helper is intentionally surface-level — it does not parse the
    SVG into a DOM and pick safe nodes; that would block legitimate
    extensions (text labels, gradients, masks) we want to keep allowing.
    The trade-off is acceptable because the routes never inline the
    payload into an HTML page; they serve it as a standalone
    ``image/svg+xml`` resource, and the few inline uses (the list
    thumbnail grid) render via ``<img src=...>`` which forbids script
    execution entirely.
    """
    cleaned = _SCRIPT_RE.sub("", payload)
    cleaned = _SCRIPT_SELF_CLOSE_RE.sub("", cleaned)
    cleaned = _ON_ATTR_RE.sub("", cleaned)
    cleaned = _JS_URI_RE.sub(r'\1=\2\2', cleaned)
    return cleaned


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an ``aiosqlite.Row`` into a JSON-serialisable dict.

    Mirrors the shape every other v1.4x memory helper returns: a flat
    dict with native ints / strs / ``None`` so the route layer never
    has to re-shape rows for ``JSONResponse``.
    """
    return {
        "id": int(row["id"]),
        "title": (str(row["title"]) if row["title"] is not None else None),
        "svg_payload": str(row["svg_payload"]),
        "width": int(row["width"]),
        "height": int(row["height"]),
        "created_at": str(row["created_at"]),
        "tags": (str(row["tags"]) if row["tags"] is not None else None),
    }


async def create_sketch(
    title: str | None,
    svg_payload: str,
    width: int,
    height: int,
    tags: str | None = None,
) -> int:
    """Insert one sketch row and return its primary key.

    The ``svg_payload`` is passed through :func:`sanitize_svg` before
    persistence so the stored value is already safe to serve back as
    ``image/svg+xml``. ``width`` and ``height`` are clamped to a floor
    of ``1`` to keep zero-area sketches (a misfire from the JS layer)
    from poisoning the list view's aspect-ratio reservation.
    """
    safe_title = title.strip() if title else None
    safe_title = safe_title or None
    safe_payload = sanitize_svg(svg_payload)
    safe_width = max(1, int(width))
    safe_height = max(1, int(height))
    safe_tags = tags.strip() if tags else None
    safe_tags = safe_tags or None

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO sketch_note "
            "(title, svg_payload, width, height, tags) "
            "VALUES (?, ?, ?, ?, ?)",
            (safe_title, safe_payload, safe_width, safe_height, safe_tags),
        )
        await conn.commit()
        new_id = int(cursor.lastrowid or 0)

    log.info(
        "sketch_notes.created",
        sketch_id=new_id,
        title=safe_title,
        width=safe_width,
        height=safe_height,
        bytes=len(safe_payload),
        tags=safe_tags,
    )
    return new_id


async def list_sketches(
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` sketches, newest first, skipping ``offset``.

    Each dict carries every column from :func:`_row_to_dict`, including
    the full ``svg_payload`` — callers that only want metadata can
    discard the field. We deliberately do not project a "preview-only"
    shape because the payload is tiny (line-art SVG is bytes, not
    kilobytes) and a second query for the full document would dominate
    any savings.
    """
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, title, svg_payload, width, height, created_at, tags "
            "FROM sketch_note "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (safe_limit, safe_offset),
        )
        rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def get_sketch(sketch_id: int) -> dict[str, Any] | None:
    """Return one sketch row by primary key, or ``None`` when missing."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, title, svg_payload, width, height, created_at, tags "
            "FROM sketch_note WHERE id = ?",
            (int(sketch_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def delete_sketch(sketch_id: int) -> None:
    """Delete a sketch by primary key. Idempotent.

    A double-delete from a stale UI is silently fine — the contract is
    "after this returns, the row is gone". The structlog event fires
    even when the row was already missing, so the operator can see
    which ids were targeted.
    """
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM sketch_note WHERE id = ?",
            (int(sketch_id),),
        )
        await conn.commit()
    log.info("sketch_notes.deleted", sketch_id=int(sketch_id))


async def update_sketch_title(sketch_id: int, title: str) -> None:
    """Replace the title of one sketch. Empty strings collapse to ``NULL``.

    The editor's rename inline-input fires this; we treat an empty /
    whitespace-only submission as "clear the title" rather than 400ing,
    matching the contract of every other rename surface in Persona.
    """
    safe_title = title.strip() if title else ""
    stored: str | None = safe_title or None
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE sketch_note SET title = ? WHERE id = ?",
            (stored, int(sketch_id)),
        )
        await conn.commit()
    log.info(
        "sketch_notes.title_updated",
        sketch_id=int(sketch_id),
        title=stored,
    )


__all__ = [
    "create_sketch",
    "delete_sketch",
    "get_sketch",
    "list_sketches",
    "sanitize_svg",
    "update_sketch_title",
]
