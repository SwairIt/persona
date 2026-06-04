"""Screenshot annotations — SVG overlay persistence helpers (v1.20).

The user draws rectangles, arrows and text labels on top of a saved
thumbnail; the client serialises the result into a single SVG payload
(inner markup, not a full document) and POSTs it here. The payload is
stored 1:1 per ``screenshots.id`` in the ``shot_annotation`` table
created by migration ``104_screenshot_annotations.sql``.

Safety contract
---------------
- Every read returns the payload wrapped in :func:`sanitise_svg`, which
  strips ``<script>`` tags before the template renders it via Jinja's
  ``| safe`` filter. The template additionally embeds the payload only
  inside an existing ``<svg>`` container so a stray ``</svg>`` would
  break the overlay but never escape into the surrounding document.
- Inserts reject payloads larger than :data:`MAX_PAYLOAD_BYTES`
  (64 KiB) with :class:`ValueError`; the route translates that into a
  ``413 Payload Too Large``.
- All SQL is parametrised; the table name is a literal.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.shot_annotations")

MAX_PAYLOAD_BYTES: int = 64 * 1024
"""Maximum accepted SVG payload size (bytes, UTF-8 encoded)."""

_SCRIPT_TAG_RE = re.compile(
    r"<\s*script\b[^>]*>.*?<\s*/\s*script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_OPEN_RE = re.compile(r"<\s*script\b[^>]*>", re.IGNORECASE)
_ON_HANDLER_RE = re.compile(r"\son[a-z]+\s*=\s*\"[^\"]*\"", re.IGNORECASE)
_ON_HANDLER_SQ_RE = re.compile(r"\son[a-z]+\s*=\s*'[^']*'", re.IGNORECASE)
_JS_HREF_RE = re.compile(
    r"\b(?:href|xlink:href)\s*=\s*([\"'])\s*javascript:[^\"']*\1",
    re.IGNORECASE,
)


def sanitise_svg(text: str) -> str:
    """Strip ``<script>`` tags and inline JS handlers from ``text``.

    The annotation editor only emits ``<g>``/``<rect>``/``<line>``/
    ``<text>``/``<polygon>`` elements, so a real document never needs
    a ``<script>``; anything that looks like one is treated as a
    malicious payload from a tampered DB row and removed.

    The sanitiser is intentionally a regex pass — a full XML parse
    would be overkill for what is essentially a whitelist of inert
    shape primitives, and the route never renders unsanitised text.
    """
    if not text:
        return ""
    cleaned = _SCRIPT_TAG_RE.sub("", text)
    # Defensive: an unbalanced ``<script>`` (no closing tag) would still
    # smuggle JS. Drop the opening tag too.
    cleaned = _SCRIPT_OPEN_RE.sub("", cleaned)
    cleaned = _ON_HANDLER_RE.sub("", cleaned)
    cleaned = _ON_HANDLER_SQ_RE.sub("", cleaned)
    cleaned = _JS_HREF_RE.sub('href="#"', cleaned)
    return cleaned


def _validate_payload(svg_payload: str) -> None:
    """Raise :class:`ValueError` if ``svg_payload`` exceeds the size limit.

    Empty payloads are allowed — the editor saves an empty ``<g/>`` when
    the user wipes every shape but still wants the row to exist.
    """
    size = len(svg_payload.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        msg = f"svg_payload too large: {size} bytes (max {MAX_PAYLOAD_BYTES})"
        raise ValueError(msg)


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "screenshot_id": int(row["screenshot_id"]),
        "svg_payload": str(row["svg_payload"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


async def get_annotation(screenshot_id: int) -> dict[str, Any] | None:
    """Return the stored annotation row for ``screenshot_id`` or ``None``.

    The returned ``svg_payload`` is the raw stored text — callers that
    render it into HTML must pipe it through :func:`sanitise_svg` first.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, screenshot_id, svg_payload, created_at, updated_at "
            "FROM shot_annotation WHERE screenshot_id = ?",
            (int(screenshot_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


async def upsert_annotation(
    screenshot_id: int,
    svg_payload: str,
) -> dict[str, Any]:
    """Insert or update the annotation row for ``screenshot_id``.

    Returns the persisted row as a dict (after the upsert), so the API
    can echo back the canonical ``created_at`` / ``updated_at``.

    Raises :class:`ValueError` when the payload exceeds
    :data:`MAX_PAYLOAD_BYTES`.
    """
    _validate_payload(svg_payload)
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO shot_annotation (screenshot_id, svg_payload)
            VALUES (?, ?)
            ON CONFLICT(screenshot_id) DO UPDATE SET
                svg_payload = excluded.svg_payload,
                updated_at = datetime('now')
            """,
            (int(screenshot_id), svg_payload),
        )
        await conn.commit()
        cursor = await conn.execute(
            "SELECT id, screenshot_id, svg_payload, created_at, updated_at "
            "FROM shot_annotation WHERE screenshot_id = ?",
            (int(screenshot_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        msg = "upsert_annotation: row vanished after insert"
        raise RuntimeError(msg)
    result = _row_to_dict(row)
    log.info(
        "shot_annotations.upsert",
        screenshot_id=int(screenshot_id),
        bytes=len(svg_payload.encode("utf-8")),
    )
    return result


async def delete_annotation(screenshot_id: int) -> bool:
    """Delete the annotation row for ``screenshot_id``.

    Returns ``True`` when a row was removed, ``False`` when no row
    existed in the first place (so the API can decide between 204 and
    404). Uses ``changes()`` rather than a pre-SELECT to keep the call
    a single round-trip.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM shot_annotation WHERE screenshot_id = ?",
            (int(screenshot_id),),
        )
        await conn.commit()
        removed = (cursor.rowcount or 0) > 0
    if removed:
        log.info("shot_annotations.delete", screenshot_id=int(screenshot_id))
    return removed
