"""Possibly-related screenshot suggestions for the detail page.

Persona v0.47 feature 2/3. Given a single screenshot, surfaces up to
four other shots that the user is likely to want next to it: same
``dedup_group_id`` first (the v0 deduper has already decided "this is
the same window state captured again"), then near pHash neighbours as
a fallback when the dedup group is too small or missing.

Why two stages?
---------------
* ``dedup_group_id`` is the high-precision signal — two shots share it
  only when :func:`app.dedup.phash.find_or_create_dedup_group` matched
  the same exact or near pHash at capture time. If the user has the
  group, we trust it completely and never need to recompute anything.
* When the group has fewer than ``limit`` members (a fresh capture, a
  cold-tier shot whose neighbours expired, a row whose ``dedup_group_id``
  is ``NULL`` because the dedup pass failed), we still want a strip
  instead of an empty area. The fallback walks recent ``dedup_groups``
  by ``last_seen DESC`` and picks the closest-pHash neighbours via
  Hamming distance — same helper the capture-time deduper already
  uses, so the threshold is consistent across the codebase.

Pure read path — no writes, no commits. The function is ``async`` only
to share the project's ``get_connection`` async context manager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Literal

from app.dedup import hamming_distance
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.dup_suggest")

SuggestReason = Literal["same_dedup_group", "phash_neighbour"]

# Hamming threshold for the pHash-neighbour fallback. Mirrors the
# default capture-time threshold so a shot the deduper would have
# merged is also surfaced as "possibly related" on the detail page,
# even when the actual dedup group lookup missed (e.g. NULL group_id
# on rows from before the deduper shipped, or freshly-captured rows
# the worker has not pHash-grouped yet).
_PHASH_NEIGHBOUR_THRESHOLD: Final[int] = 8

# Cap on the candidate pool we Hamming-scan for the fallback. The
# deduper itself walks ``list_recent_dedup_groups(limit=200)``; we
# scan recent *screenshots* directly to surface specific row ids the
# UI can link to, and cap at 200 so even with a 100k-row database the
# inner loop stays under a millisecond.
_PHASH_CANDIDATE_LIMIT: Final[int] = 200


async def suggest_similar(
    shot_id: int,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` shots visually similar to ``shot_id``.

    Each returned dict has::

        {
            "id": int,
            "captured_at": str,        # ISO-8601, exactly as stored
            "app_name": str | None,
            "thumbnail_url": str | None,  # "/thumbs/..." or None
            "reason": "same_dedup_group" | "phash_neighbour",
        }

    Empty list when ``shot_id`` does not exist. Never raises for a
    missing row — the detail page strip should silently disappear,
    not 500 the request.

    Two-stage algorithm:

    1. If ``dedup_group_id`` is non-NULL, select other members of the
       same group ordered by ``captured_at DESC`` and cap at ``limit``.
    2. If stage 1 returned fewer than ``limit``, scan up to
       :data:`_PHASH_CANDIDATE_LIMIT` recent screenshots, filter to
       those whose pHash is within :data:`_PHASH_NEIGHBOUR_THRESHOLD`
       Hamming distance, exclude the seed shot + any ids already
       picked in stage 1, sort by ascending distance and use the top
       ``limit - len(stage1)`` to top up the result.
    """
    if limit <= 0:
        return []

    async with get_connection() as conn:
        seed = await _fetch_seed(conn, shot_id)
        if seed is None:
            log.debug("dup_suggest.seed_missing", shot_id=shot_id)
            return []

        results: list[dict[str, Any]] = []
        picked_ids: set[int] = {shot_id}

        if seed["dedup_group_id"] is not None:
            group_rows = await _fetch_same_group(
                conn,
                group_id=int(seed["dedup_group_id"]),
                exclude_id=shot_id,
                limit=limit,
            )
            for row in group_rows:
                results.append(_row_to_suggestion(row, reason="same_dedup_group"))
                picked_ids.add(int(row["id"]))

        if len(results) < limit:
            needed = limit - len(results)
            neighbour_rows = await _fetch_phash_neighbours(
                conn,
                seed_phash=str(seed["phash"]),
                exclude_ids=picked_ids,
                needed=needed,
            )
            for row in neighbour_rows:
                results.append(_row_to_suggestion(row, reason="phash_neighbour"))

    log.debug(
        "dup_suggest.done",
        shot_id=shot_id,
        returned=len(results),
        had_group=seed["dedup_group_id"] is not None,
    )
    return results[:limit]


async def _fetch_seed(
    conn: aiosqlite.Connection,
    shot_id: int,
) -> aiosqlite.Row | None:
    """Pull just the columns we need from the seed row."""
    cursor = await conn.execute(
        "SELECT id, phash, dedup_group_id FROM screenshots WHERE id = ?",
        (shot_id,),
    )
    return await cursor.fetchone()


async def _fetch_same_group(
    conn: aiosqlite.Connection,
    *,
    group_id: int,
    exclude_id: int,
    limit: int,
) -> list[aiosqlite.Row]:
    """Other members of the same dedup group, newest first."""
    cursor = await conn.execute(
        "SELECT id, captured_at, app_name, thumbnail_path "
        "FROM screenshots "
        "WHERE dedup_group_id = ? AND id != ? "
        "ORDER BY captured_at DESC "
        "LIMIT ?",
        (group_id, exclude_id, limit),
    )
    return list(await cursor.fetchall())


async def _fetch_phash_neighbours(
    conn: aiosqlite.Connection,
    *,
    seed_phash: str,
    exclude_ids: set[int],
    needed: int,
) -> list[aiosqlite.Row]:
    """Recent shots whose pHash is within the Hamming threshold.

    We pull a capped candidate pool and rank in Python — SQLite has no
    native ``hamming(a, b)`` and registering a per-connection function
    would leak into every other query path. The cap keeps the scan
    cheap even on a large database.
    """
    if needed <= 0:
        return []

    cursor = await conn.execute(
        "SELECT id, captured_at, app_name, thumbnail_path, phash "
        "FROM screenshots "
        "ORDER BY captured_at DESC "
        "LIMIT ?",
        (_PHASH_CANDIDATE_LIMIT,),
    )
    rows = await cursor.fetchall()

    scored: list[tuple[int, aiosqlite.Row]] = []
    for row in rows:
        row_id = int(row["id"])
        if row_id in exclude_ids:
            continue
        candidate_phash = str(row["phash"])
        try:
            distance = hamming_distance(seed_phash, candidate_phash)
        except ValueError:
            # Different pHash length (e.g. legacy rows from before the
            # default hash_size was settled). Skip rather than fail —
            # one corrupt row should not nuke the whole strip.
            continue
        if distance <= _PHASH_NEIGHBOUR_THRESHOLD:
            scored.append((distance, row))

    scored.sort(key=lambda pair: pair[0])
    return [row for _, row in scored[:needed]]


def _row_to_suggestion(
    row: aiosqlite.Row,
    *,
    reason: SuggestReason,
) -> dict[str, Any]:
    """Shape a raw row into the public suggestion dict."""
    # Lazy import — :mod:`app.web.routes.thumbnails` imports back into
    # the storage layer and a top-level import here would close the
    # cycle. Same guard ``templates_engine`` already uses.
    from app.web.routes.thumbnails import thumbnail_url  # noqa: PLC0415

    raw_path = row["thumbnail_path"]
    thumb_url = thumbnail_url(raw_path) if raw_path is not None else None

    return {
        "id": int(row["id"]),
        "captured_at": str(row["captured_at"]),
        "app_name": row["app_name"],
        "thumbnail_url": thumb_url,
        "reason": reason,
    }
