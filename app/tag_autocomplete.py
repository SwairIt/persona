"""Tag autocomplete — fast prefix-match lookup across screenshot + sketch tags.

The palette, the sketch-note editor, and every "type a hashtag" input on the
site want the same thing: given a 1-3 character prefix, return the most
frequently-used tags that start with it so the user can pick one with a
keyboard tap instead of typing it out (and risking a typo that creates a
near-duplicate).

We pull candidates from two surfaces:

* ``screenshot_tags`` joined onto ``tags`` — the canonical tag inventory; one
  row per (shot, tag) pair, so a ``GROUP BY`` gives us the per-tag usage count
  for sort order.
* ``sketch_note.tags`` — the comma-separated tag list each sketch carries
  inline (the schema comment in :file:`135_sketch_note.sql` is explicit that
  sketches reuse the cheap comma-form rather than a join table). We pull the
  raw column values, split client-side in Python, and tally.

The two streams are merged on the *normalised* tag name (lowercased, stripped)
and the ``source`` field reports where the count came from: ``"shots"``,
``"sketches"``, or ``"both"``. The final list is ordered by descending count,
ties broken alphabetically so the response is deterministic across calls.

All SQL is parametrised — the user-supplied prefix is escaped against SQLite's
``LIKE`` wildcards (``%`` and ``_``) before the ``%`` suffix is appended.
"""

from __future__ import annotations

from typing import Final, Literal, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_autocomplete")


# Upper bound on the ``limit`` parameter accepted by :func:`suggest_tags`. The
# autocomplete dropdown only ever surfaces a handful of rows; capping the SQL
# limit defensively keeps a hostile caller from asking for ten thousand rows
# at once.
_MAX_LIMIT: Final[int] = 100

# Default ``limit`` value when the caller does not specify one. Matches the
# typical "show me the top 10 matches" UX of an autocomplete dropdown.
_DEFAULT_LIMIT: Final[int] = 10

# Hard cap on the prefix length we accept. Anything past this is almost
# certainly noise (the underlying tag column is capped at 64 chars in
# :mod:`app.web.routes.hashtag_suggest`); a long prefix would also generate a
# ``LIKE`` pattern that matches nothing, so we clip rather than reject.
_MAX_PREFIX_LENGTH: Final[int] = 64

# Cap on the number of sketch rows we scan for the in-Python tag tally. The
# sketch table is low-volume by design (per the schema notes in
# :file:`135_sketch_note.sql`) so this bound is generous; it exists only to
# stop a runaway database from turning autocomplete into a full-table scan.
_SKETCH_SCAN_CAP: Final[int] = 5000


Source = Literal["shots", "sketches", "both"]


class TagSuggestion(TypedDict):
    """One ranked tag suggestion."""

    tag: str
    count: int
    source: Source


def _normalise_prefix(prefix: str) -> str:
    """Lowercase + strip, then clip to :data:`_MAX_PREFIX_LENGTH`.

    Matches the slugifier in :mod:`app.web.routes.hashtag_suggest` for the
    leading edge — we only need the canonical lowercase form here because the
    ``tags.name`` column is already stored lowercased by the writer.
    """
    return prefix.strip().lower()[:_MAX_PREFIX_LENGTH]


def _escape_like(value: str) -> str:
    """Escape SQLite's ``LIKE`` wildcards so a typed ``%`` is literal.

    SQLite's ``LIKE`` treats ``%`` and ``_`` as wildcards; an unescaped
    underscore (common in tags like ``project_doday``) would match any single
    character and pollute the result list. We escape with backslash and tell
    the query to use ``ESCAPE '\\'`` so the parametrised binding still works.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_limit(limit: int) -> int:
    """Clamp ``limit`` into ``[1, _MAX_LIMIT]``.

    A caller passing ``0`` or a negative number gets the default; anything
    above the hard cap gets clipped silently. We never raise here — the route
    layer is the right place to reject obviously-bogus inputs.
    """
    if limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _split_sketch_tags(raw: str | None) -> list[str]:
    """Split a sketch_note.tags comma-form string into normalised entries.

    The schema comment in :file:`135_sketch_note.sql` is explicit that sketch
    tags live in a single comma-separated TEXT column rather than a join
    table. We split, strip, lowercase, and drop empties so a row stored as
    ``" Design, ui , "`` yields ``["design", "ui"]``.
    """
    if not raw:
        return []
    out: list[str] = []
    for piece in raw.split(","):
        cleaned = piece.strip().lower()
        if cleaned:
            out.append(cleaned)
    return out


async def _shots_counts(
    prefix: str,
    limit: int,
) -> dict[str, int]:
    """Aggregate ``screenshot_tags`` rows whose tag name starts with ``prefix``.

    The query joins ``screenshot_tags`` onto ``tags`` so we can match on the
    canonical lowercased ``tags.name`` column and return the per-tag usage
    count. ``LIKE`` with the escaped prefix plus a trailing ``%`` does the
    prefix match; the ``GROUP BY`` collapses the join rows into one entry per
    tag, ``ORDER BY`` ranks by usage so the top-N are the most-used.
    """
    pattern = _escape_like(prefix) + "%"
    out: dict[str, int] = {}
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT t.name AS tag, COUNT(*) AS n
            FROM screenshot_tags st
            JOIN tags t ON t.id = st.tag_id
            WHERE t.name LIKE ? ESCAPE '\\'
            GROUP BY t.name
            ORDER BY n DESC, t.name ASC
            LIMIT ?
            """,
            (pattern, limit),
        )
        async for row in cursor:
            tag_value = row["tag"]
            if tag_value is None:
                continue
            tag = str(tag_value).strip().lower()
            if not tag:
                continue
            out[tag] = int(row["n"])
    return out


async def _sketch_counts(prefix: str) -> dict[str, int]:
    """Tally sketch_note tag entries whose normalised value starts with ``prefix``.

    The sketch surface stores tags inline as comma-separated TEXT, so a SQL
    ``GROUP BY`` on the raw column would group ``"design,ui"`` distinct from
    ``"design,wireframe"`` rather than counting ``"design"`` twice. We instead
    pull rows that *contain* the prefix as a substring (cheap server-side
    filter) and finish the split + per-tag count in Python.

    The substring filter is a superset of the prefix filter — it also matches
    rows where the prefix lands mid-string (e.g. ``"redesign"`` for prefix
    ``des``) — so the Python loop enforces the actual ``startswith`` check on
    each individual tag inside the comma-form string.
    """
    pattern = "%" + _escape_like(prefix) + "%"
    out: dict[str, int] = {}
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT tags
            FROM sketch_note
            WHERE tags IS NOT NULL
              AND tags <> ''
              AND LOWER(tags) LIKE ? ESCAPE '\\'
            ORDER BY id DESC
            LIMIT ?
            """,
            (pattern, _SKETCH_SCAN_CAP),
        )
        async for row in cursor:
            for tag in _split_sketch_tags(
                None if row["tags"] is None else str(row["tags"])
            ):
                if not tag.startswith(prefix):
                    continue
                out[tag] = out.get(tag, 0) + 1
    return out


def _merge_sources(
    shots: dict[str, int],
    sketches: dict[str, int],
    limit: int,
) -> list[TagSuggestion]:
    """Combine the two count maps into a ranked, deterministic suggestion list.

    Counts sum across surfaces — if ``"design"`` shows up 12 times in
    screenshots and 4 times in sketches, the merged count is 16 and the
    ``source`` is ``"both"``. Ties are broken alphabetically so the response
    is stable across calls (important for snapshot tests and for keyboard
    autocomplete UX where re-orderings between keystrokes are jarring).
    """
    merged: dict[str, tuple[int, Source]] = {}
    for tag, count in shots.items():
        merged[tag] = (count, "shots")
    for tag, count in sketches.items():
        if tag in merged:
            prev_count, _prev_source = merged[tag]
            merged[tag] = (prev_count + count, "both")
        else:
            merged[tag] = (count, "sketches")

    ranked = sorted(
        merged.items(),
        key=lambda pair: (-pair[1][0], pair[0]),
    )
    return [
        TagSuggestion(tag=tag, count=count, source=source)
        for tag, (count, source) in ranked[:limit]
    ]


async def suggest_tags(
    prefix: str,
    limit: int = _DEFAULT_LIMIT,
) -> list[TagSuggestion]:
    """Return ranked tag suggestions whose names start with ``prefix``.

    Args:
        prefix: User input — case-insensitive, surrounding whitespace
            tolerated. An empty prefix is treated as "any tag" and returns
            the most-used tags overall (useful for the palette's "show
            recent tags" surface).
        limit: Soft cap on the returned list. Clipped to
            ``[1, _MAX_LIMIT]``; a non-positive value falls back to the
            module default of 10.

    Returns:
        A list of :class:`TagSuggestion` rows ordered by descending count,
        ties broken alphabetically. ``source`` reports whether the count
        came from screenshot tags, sketch tags, or both.
    """
    normalised = _normalise_prefix(prefix)
    capped_limit = _clamp_limit(limit)

    shots = await _shots_counts(normalised, capped_limit)
    sketches = await _sketch_counts(normalised)
    merged = _merge_sources(shots, sketches, capped_limit)

    log.info(
        "tag_autocomplete.suggest",
        prefix=normalised,
        limit=capped_limit,
        shots_matches=len(shots),
        sketches_matches=len(sketches),
        returned=len(merged),
    )
    return merged


async def all_tags(limit: int = 500) -> list[TagSuggestion]:
    """Return every tag with its merged count, capped at ``limit`` rows.

    Thin wrapper around :func:`suggest_tags` with an empty prefix — kept as a
    separate entrypoint so the ``/api/tags/all`` route reads cleanly and so
    future callers (export, audit dump) can grab the full inventory without
    threading a magic empty-string argument through their call site.
    """
    return await suggest_tags("", limit=limit)


__all__ = [
    "TagSuggestion",
    "all_tags",
    "suggest_tags",
]
