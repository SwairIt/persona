"""Lightweight per-app favicon chip cache — glyph + colour, not PNG.

Sibling to the heavier PNG cache in :mod:`app.app_icons`. That module
serves rasterised 64x64 tiles extracted from running exes via
``SHGetFileInfoW``; this one serves a tiny "favicon chip" — a single
emoji or letter plus a tailwind-friendly hex colour — for visual
identification in dense list views (timeline rows, autocomplete
suggestions, search results) where firing a PNG fetch per row would be
both wasteful and visually noisy.

Lookup contract (:func:`ensure_icon_for`):

1. Existing row in ``app_icon_chip`` keyed by the normalised app name.
   Returned as-is; user overrides therefore take precedence simply by
   virtue of being the row that's there.
2. Miss + lowercased name matches a key in :data:`BUNDLED_ICONS`.
   INSERT a ``bundled`` row carrying that entry's glyph + colour.
3. Miss + no bundled match. INSERT a ``fallback`` row carrying the
   uppercased first letter and a colour derived from a SHA-256 hash of
   the lowercased name. Deterministic across machines and reboots so
   shared backups / screenshots reuse the same colours.

Schema note: the ``icon_path`` column stores the glyph string. The name
``icon_path`` was kept from the original schema brief; we overload it
for the chip glyph (1-2 unicode chars) so a future "upload an SVG
override" path can land without a follow-up migration. The
"path" naming is historical, not semantic.

All DB I/O is parametrised (no SQL string interpolation) and wrapped in
the async ``aiosqlite`` connection helper so a calling coroutine never
blocks the event loop on disk I/O.
"""

from __future__ import annotations

import hashlib
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.app_icons")


class _BundledEntry(TypedDict):
    """Shape of a single :data:`BUNDLED_ICONS` value.

    ``icon_color`` is a CSS-ready hex string (``#rrggbb``); ``glyph`` is
    a single unicode emoji or letter sized to read at chip dimensions.
    """

    icon_color: str
    glyph: str


class IconRow(TypedDict):
    """Shape of a row returned by :func:`ensure_icon_for` / :func:`list_icons`.

    ``icon_path`` carries the glyph (see module docstring for the naming
    rationale). ``source`` is one of ``fallback`` / ``bundled`` /
    ``user`` — matches the ``CHECK`` constraint in migration 143.
    """

    app_name: str
    icon_path: str
    icon_color: str
    source: str


# ---------------------------------------------------------------------------
# Bundled defaults
# ---------------------------------------------------------------------------
# Mapping from lowercased ``app_name`` to a hand-picked glyph + brand-ish
# colour. Lookup is case-insensitive — see :func:`_match_bundled`. Keys
# are kept short (no ``.exe`` suffix) so a row like ``Chrome`` and
# ``chrome.exe`` collapse to the same entry. The colour palette skews
# saturated-but-readable so a white glyph reads on every tile.

BUNDLED_ICONS: Final[dict[str, _BundledEntry]] = {
    "vscode": {"icon_color": "#0078d4", "glyph": "\U0001f4dd"},  # memo
    "code": {"icon_color": "#0078d4", "glyph": "\U0001f4dd"},
    "chrome": {"icon_color": "#4285f4", "glyph": "\U0001f310"},  # globe
    "firefox": {"icon_color": "#ff7139", "glyph": "\U0001f98a"},  # fox
    "safari": {"icon_color": "#1b88ca", "glyph": "\U0001f9ed"},  # compass
    "slack": {"icon_color": "#611f69", "glyph": "\U0001f4ac"},  # speech
    "discord": {"icon_color": "#5865f2", "glyph": "\U0001f3ae"},  # game pad
    "zoom": {"icon_color": "#2d8cff", "glyph": "\U0001f3a5"},  # video camera
    "teams": {"icon_color": "#6264a7", "glyph": "\U0001f465"},  # busts
    "mail": {"icon_color": "#0a84ff", "glyph": "✉️"},  # envelope
    "spotify": {"icon_color": "#1db954", "glyph": "\U0001f3b5"},  # note
    "terminal": {"icon_color": "#1f2937", "glyph": "▶️"},  # play
    "figma": {"icon_color": "#a259ff", "glyph": "\U0001f3a8"},  # palette
    "notion": {"icon_color": "#111111", "glyph": "\U0001f4d3"},  # notebook
    "linear": {"icon_color": "#5e6ad2", "glyph": "\U0001f4ca"},  # chart
    "github desktop": {"icon_color": "#24292e", "glyph": "\U0001f419"},  # octopus
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_FALLBACK: Final[str] = "fallback"
_SOURCE_BUNDLED: Final[str] = "bundled"
_SOURCE_USER: Final[str] = "user"

# Hex palette for the deterministic-fallback path. Each entry is paired
# with the white glyph in templates; we picked twelve hues spaced around
# the colour wheel so adjacent rows in a list don't collide.
_FALLBACK_PALETTE: Final[tuple[str, ...]] = (
    "#ef4444",  # red
    "#f97316",  # orange
    "#f59e0b",  # amber
    "#84cc16",  # lime
    "#22c55e",  # green
    "#14b8a6",  # teal
    "#06b6d4",  # cyan
    "#3b82f6",  # blue
    "#6366f1",  # indigo
    "#a855f7",  # purple
    "#ec4899",  # pink
    "#64748b",  # slate
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ensure_icon_for(app_name: str) -> IconRow:
    """Return the chip row for ``app_name``, creating it on first miss.

    Idempotent: subsequent calls return the same row by ``app_name``
    primary key, no extra INSERT. Empty / whitespace input is normalised
    to an empty key but still yields a deterministic fallback chip so
    the caller never has to special-case it.
    """
    key = _normalise(app_name)

    existing = await _select_one(key)
    if existing is not None:
        return existing

    bundled = _match_bundled(key)
    if bundled is not None:
        glyph = bundled["glyph"]
        colour = bundled["icon_color"]
        source = _SOURCE_BUNDLED
    else:
        glyph = _fallback_glyph(key)
        colour = _fallback_colour(key)
        source = _SOURCE_FALLBACK

    await _insert(key, glyph, colour, source)
    log.info(
        "app_icons.ensured",
        app_name=key,
        source=source,
        glyph=glyph,
        icon_color=colour,
    )
    # Re-select so the caller receives the row exactly as the DB sees it
    # (defaults filled in, ``created_at`` populated, no drift between
    # the in-memory dict and the row that ``list_icons`` will later
    # return). Cheap: same connection pool, single-row PK lookup.
    fresh = await _select_one(key)
    if fresh is not None:
        return fresh
    # Defensive: should never happen because we just inserted with the
    # same key, but keep the function total rather than raising on a
    # race that we can synthesise a value for.
    return IconRow(
        app_name=key,
        icon_path=glyph,
        icon_color=colour,
        source=source,
    )


async def list_icons() -> list[IconRow]:
    """Return every chip row, alphabetically by app_name.

    Used by the settings page to render the grid. Cheap on small tables
    (one row per known app); a future indexed query can layer on
    pagination if the install grows beyond a few hundred apps.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, icon_path, icon_color, source "
            "FROM app_icon_chip ORDER BY app_name ASC",
        )
        rows = await cursor.fetchall()
    return [_row_to_icon(row) for row in rows]


async def set_user_icon(app_name: str, glyph: str, color: str) -> None:
    """Persist an operator-chosen glyph + colour with ``source='user'``.

    UPSERT semantics: overwrites any previous row for ``app_name`` so
    the operator can flip between glyphs without an intermediate reset.
    Caller is responsible for validation (single grapheme glyph, hex
    colour shape) — this helper is the storage primitive.
    """
    key = _normalise(app_name)
    cleaned_glyph = (glyph or "").strip() or _fallback_glyph(key)
    cleaned_colour = (color or "").strip() or _fallback_colour(key)

    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_icon_chip (app_name, icon_path, icon_color, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(app_name) DO UPDATE SET
                icon_path = excluded.icon_path,
                icon_color = excluded.icon_color,
                source = excluded.source
            """,
            (key, cleaned_glyph, cleaned_colour, _SOURCE_USER),
        )
        await conn.commit()
    log.info(
        "app_icons.user_set",
        app_name=key,
        glyph=cleaned_glyph,
        icon_color=cleaned_colour,
    )


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


async def _select_one(app_name: str) -> IconRow | None:
    """Fetch the single row for ``app_name`` or ``None`` if absent."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, icon_path, icon_color, source "
            "FROM app_icon_chip WHERE app_name = ?",
            (app_name,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_icon(row)


async def _insert(app_name: str, glyph: str, colour: str, source: str) -> None:
    """Insert a row, ignoring conflicts (another coroutine raced us)."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_icon_chip (app_name, icon_path, icon_color, source)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(app_name) DO NOTHING
            """,
            (app_name, glyph, colour, source),
        )
        await conn.commit()


def _row_to_icon(row: object) -> IconRow:
    """Convert an ``aiosqlite.Row`` into the typed :class:`IconRow` dict.

    Pulled out so the SELECT helpers stay one-liners and the cast lives
    in exactly one place. ``aiosqlite.Row`` supports both index and
    column-name access — we use the column-name form for readability.
    """
    return IconRow(
        app_name=str(row["app_name"]),  # type: ignore[index]
        icon_path=str(row["icon_path"] or ""),  # type: ignore[index]
        icon_color=str(row["icon_color"] or ""),  # type: ignore[index]
        source=str(row["source"]),  # type: ignore[index]
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalise(app_name: str) -> str:
    """Trim and lowercase the cache key. Mirrors :mod:`app.app_icons`."""
    return (app_name or "").strip().lower()


def _match_bundled(key: str) -> _BundledEntry | None:
    """Return the BUNDLED_ICONS entry for ``key`` or None.

    Accepts exact match *and* exe-stem match so ``chrome.exe`` resolves
    to the ``chrome`` entry without the caller pre-stripping the suffix.
    """
    if not key:
        return None
    direct = BUNDLED_ICONS.get(key)
    if direct is not None:
        return direct
    stem = key[:-4] if key.endswith(".exe") else key
    return BUNDLED_ICONS.get(stem)


def _fallback_glyph(key: str) -> str:
    """Return the uppercased first alphanumeric character of ``key``.

    Falls back to ``"?"`` for empty / unusable input so the chip
    renderer never has to handle an empty string.
    """
    for ch in key:
        if ch.isalnum():
            return ch.upper()
    return "?"


def _fallback_colour(key: str) -> str:
    """Pick a stable colour from :data:`_FALLBACK_PALETTE` for ``key``.

    SHA-256 over the lowercased key so the choice is deterministic
    across processes and machines — the same app gets the same colour
    on every install, which matters for shared screenshots of the UI.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return _FALLBACK_PALETTE[digest[0] % len(_FALLBACK_PALETTE)]


__all__ = [
    "BUNDLED_ICONS",
    "IconRow",
    "ensure_icon_for",
    "list_icons",
    "set_user_icon",
]
