"""Emoji reactions on screenshots — toggle, list and top-N helpers.

Five-emoji vocabulary deliberately fixed in code (not a free-text column):
``love``, ``important``, ``funny``, ``wtf``, ``idea`` — mapped to the
glyphs in :data:`ALLOWED_EMOJI`. Anything else is rejected with
``ValueError`` so callers can't slip a thumbs-down into the dataset and
then expect the /reactions page to surface it.

A reaction is a *toggle*, not a counter: the schema enforces a unique
``(screenshot_id, emoji)`` so :func:`toggle_reaction` is implemented as
``INSERT OR IGNORE`` followed by a ``DELETE`` when the insert was a
no-op. The HTTP route returns the resulting ``action`` (``added`` /
``removed``) plus the new total reaction count for the shot so the
client can update its UI in one round trip.

Every helper opens its own connection via
:func:`app.storage.db.get_connection` to mirror the call style used by
:mod:`app.bulk_favourite` and :mod:`app.clipboard_embeddings` — modules
that are also direct entry points from the HTTP layer rather than being
re-used inside a larger transaction. All SQL is parameterised; there's
no string interpolation against user input.
"""

from __future__ import annotations

from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shot_reactions")

# Five-emoji fixed vocabulary. The order is the canonical UI order
# (left-to-right on the /reactions page filter strip and on the
# per-shot reaction picker). Keep it stable — clients persist filter
# state by emoji string, not by index.
ALLOWED_EMOJI: Final[tuple[str, ...]] = (
    "❤️",  # heavy black heart + variation selector — "love"
    "⭐",        # star — "important"
    "\U0001f602",    # face with tears of joy — "funny"
    "\U0001f92f",    # exploding head — "wtf"
    "\U0001f4a1",    # light bulb — "idea"
)


def _validate_emoji(emoji: str) -> None:
    """Raise ``ValueError`` if ``emoji`` is not in :data:`ALLOWED_EMOJI`.

    Centralised so every public helper enforces the vocabulary
    identically — the HTTP layer can then trust the value it stores.
    """
    if emoji not in ALLOWED_EMOJI:
        msg = f"emoji {emoji!r} is not in the allowed reaction vocabulary"
        raise ValueError(msg)


async def toggle_reaction(shot_id: int, emoji: str) -> dict[str, Any]:
    """Toggle ``emoji`` on screenshot ``shot_id``.

    Inserts the ``(shot_id, emoji)`` row if missing, otherwise removes
    it. Returns a payload describing what happened plus the new total
    reaction count across all emojis for the shot:

    .. code-block:: python

        {
            "action": "added" | "removed",
            "emoji": str,
            "shot_id": int,
            "total_for_shot": int,
        }

    Rejects an emoji outside :data:`ALLOWED_EMOJI` with ``ValueError``
    before touching the database.
    """
    _validate_emoji(emoji)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT OR IGNORE INTO shot_reaction (screenshot_id, emoji) "
            "VALUES (?, ?)",
            (shot_id, emoji),
        )
        inserted = (cursor.rowcount or 0) > 0
        action = "added"
        if not inserted:
            await conn.execute(
                "DELETE FROM shot_reaction "
                "WHERE screenshot_id = ? AND emoji = ?",
                (shot_id, emoji),
            )
            action = "removed"
        await conn.commit()

        count_cursor = await conn.execute(
            "SELECT COUNT(*) FROM shot_reaction WHERE screenshot_id = ?",
            (shot_id,),
        )
        row = await count_cursor.fetchone()
        total = int(row[0]) if row is not None else 0

    log.info(
        "shot_reactions.toggled",
        screenshot_id=shot_id,
        emoji=emoji,
        action=action,
        total_for_shot=total,
    )
    return {
        "action": action,
        "emoji": emoji,
        "shot_id": shot_id,
        "total_for_shot": total,
    }


async def list_reactions_for_shot(shot_id: int) -> list[dict[str, Any]]:
    """Return every reaction row for the given screenshot, oldest first.

    Each item carries ``id``, ``screenshot_id``, ``emoji`` and
    ``created_at`` — the same shape the HTTP route returns as JSON.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, screenshot_id, emoji, created_at "
            "FROM shot_reaction "
            "WHERE screenshot_id = ? "
            "ORDER BY id ASC",
            (shot_id,),
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row[0]),
            "screenshot_id": int(row[1]),
            "emoji": str(row[2]),
            "created_at": str(row[3]),
        }
        for row in rows
    ]


async def top_reacted_shots(
    emoji: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the ``limit`` most-reacted screenshots, optionally filtered.

    When ``emoji`` is ``None`` the aggregate counts *every* reaction
    glyph on each shot; otherwise only rows matching that single emoji
    are counted. The result rows expose the screenshot fields the
    ``reactions.html`` template needs (thumbnail, captured-at, window
    title, app name) joined with the per-shot ``reaction_count``.

    ``limit`` is clamped to a non-negative integer; pass ``0`` to get an
    empty list back without raising.
    """
    if emoji is not None:
        _validate_emoji(emoji)

    safe_limit = max(0, int(limit))
    if safe_limit == 0:
        return []

    params: tuple[Any, ...]
    if emoji is None:
        sql = (
            "SELECT s.id, s.captured_at, s.thumbnail_path, "
            "       s.window_title, s.app_name, "
            "       COUNT(r.id) AS reaction_count "
            "FROM shot_reaction AS r "
            "JOIN screenshots AS s ON s.id = r.screenshot_id "
            "GROUP BY s.id "
            "ORDER BY reaction_count DESC, s.captured_at DESC "
            "LIMIT ?"
        )
        params = (safe_limit,)
    else:
        sql = (
            "SELECT s.id, s.captured_at, s.thumbnail_path, "
            "       s.window_title, s.app_name, "
            "       COUNT(r.id) AS reaction_count "
            "FROM shot_reaction AS r "
            "JOIN screenshots AS s ON s.id = r.screenshot_id "
            "WHERE r.emoji = ? "
            "GROUP BY s.id "
            "ORDER BY reaction_count DESC, s.captured_at DESC "
            "LIMIT ?"
        )
        params = (emoji, safe_limit)

    async with get_connection() as conn:
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()

    return [
        {
            "id": int(row[0]),
            "captured_at": str(row[1]),
            "thumbnail_path": (str(row[2]) if row[2] is not None else None),
            "window_title": (str(row[3]) if row[3] is not None else None),
            "app_name": (str(row[4]) if row[4] is not None else None),
            "reaction_count": int(row[5]),
        }
        for row in rows
    ]
