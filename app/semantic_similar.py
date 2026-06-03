"""Semantically-similar screenshot suggestions for the detail page.

Persona v0.68 feature 1/3. Given one screenshot id, return the *k*
shots whose stored OCR-text embeddings are closest in meaning to the
seed shot's own embedding. Powers the "Semantically related" strip on
:file:`screenshot.html` rendered just below the existing v0.47
"Possibly related" (dedup-group / pHash) strip.

Why a separate module?
----------------------
The v0.47 helper :mod:`app.dup_suggest` works on **visual** similarity
(pHash + dedup group) — it surfaces shots that *look* the same. This
one works on **semantic** similarity (embedding cosine over OCR text)
— it surfaces shots that *talk about* the same thing even when the
screen layout is completely different.

Algorithm
---------
1. Load the seed shot's embedding vector from ``screenshot_embeddings``.
   If the seed has no embedding yet (OCR not done, no text, indexer
   behind) we return ``[]`` — the strip silently hides itself.
2. Load every other stored embedding row joined with its parent
   ``screenshots`` row (id / captured_at / app_name). For the v0.47
   sibling :mod:`app.dup_suggest`, this brute-force scan is the same
   approach :func:`app.embeddings.search.semantic_search` already uses
   — fine for the <100k-row range the project targets.
3. Compute cosine similarity in numpy if it is importable, otherwise
   bail out with an empty list. The spec explicitly says "try-import;
   if missing return empty" — we don't fall back to the slow stdlib
   loop because the surrounding feature is opt-in eye candy, not a
   correctness path.
4. Sort by descending similarity, drop the seed, return the top *k*
   dicts shaped as ``{id, captured_at, app_name, similarity}``.

Pure read path — no writes, no commits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from app.embeddings.storage import decode_vector
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.semantic_similar")

# Hard cap on the candidate pool we score. Cosine over a 384-dim vector
# is microseconds in numpy, but pulling every row from a 100k-shot DB
# still costs IO + a Python list per row. The cap keeps the worst case
# bounded; future work can swap this for an approximate index.
_CANDIDATE_LIMIT: Final[int] = 5000

# Minimum cosine to consider a row "related" at all. Mirrors the floor
# used by :func:`app.embeddings.search.semantic_search` so the two
# semantic surfaces stay consistent — anything below this is noise.
_MIN_SIMILARITY: Final[float] = 0.15


async def similar_to(
    shot_id: int,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` screenshots semantically closest to ``shot_id``.

    Each entry is shaped as::

        {
            "id": int,
            "captured_at": str,    # ISO-8601, exactly as stored
            "app_name": str | None,
            "similarity": float,   # rounded to 4 decimals, descending
        }

    Returns ``[]`` when:

    * ``limit <= 0``
    * the seed shot has no embedding row yet
    * numpy is not importable in the current environment
    * no other shot meets the :data:`_MIN_SIMILARITY` floor

    Never raises for a missing seed — the detail-page strip just hides
    itself rather than 500'ing the request.
    """
    if limit <= 0:
        return []

    try:
        import numpy as np  # noqa: PLC0415 — optional dep, must not crash import
    except ImportError:
        log.info("semantic_similar.numpy_missing", shot_id=shot_id)
        return []

    async with get_connection() as conn:
        seed_vector = await _fetch_seed_vector(conn, shot_id)
        if seed_vector is None:
            log.debug("semantic_similar.seed_missing", shot_id=shot_id)
            return []

        rows = await _fetch_candidate_rows(conn, exclude_id=shot_id)

    if not rows:
        log.debug("semantic_similar.no_candidates", shot_id=shot_id)
        return []

    seed_arr = np.asarray(seed_vector, dtype=np.float32)
    seed_norm = float(np.linalg.norm(seed_arr))
    if seed_norm == 0.0:
        log.debug("semantic_similar.zero_norm_seed", shot_id=shot_id)
        return []

    scored: list[dict[str, Any]] = []
    for row in rows:
        cand_vector = decode_vector(bytes(row["vector"]))
        if len(cand_vector) != len(seed_vector):
            # Different embedding model dimensions — skip rather than
            # crash. Mirrors the defensive guard in semantic_search.
            continue
        cand_arr = np.asarray(cand_vector, dtype=np.float32)
        cand_norm = float(np.linalg.norm(cand_arr))
        if cand_norm == 0.0:
            continue
        similarity = float(np.dot(seed_arr, cand_arr) / (seed_norm * cand_norm))
        if similarity < _MIN_SIMILARITY:
            continue
        scored.append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "app_name": row["app_name"],
                "similarity": round(similarity, 4),
            }
        )

    scored.sort(key=lambda hit: hit["similarity"], reverse=True)
    top = scored[:limit]

    log.debug(
        "semantic_similar.done",
        shot_id=shot_id,
        scanned=len(rows),
        kept=len(scored),
        returned=len(top),
    )
    return top


async def _fetch_seed_vector(
    conn: aiosqlite.Connection,
    shot_id: int,
) -> list[float] | None:
    """Return the seed shot's embedding vector, or ``None`` if absent."""
    cursor = await conn.execute(
        "SELECT vector FROM screenshot_embeddings WHERE screenshot_id = ?",
        (shot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return decode_vector(bytes(row["vector"]))


async def _fetch_candidate_rows(
    conn: aiosqlite.Connection,
    *,
    exclude_id: int,
) -> list[aiosqlite.Row]:
    """All other embedded shots, capped to :data:`_CANDIDATE_LIMIT`.

    Ordered by ``captured_at DESC`` so when the cap kicks in we keep
    the most recent material — that mirrors what users browsing the
    detail page expect ("show me lately-similar moments").
    """
    cursor = await conn.execute(
        "SELECT s.id, s.captured_at, s.app_name, e.vector "
        "FROM screenshot_embeddings e "
        "JOIN screenshots s ON s.id = e.screenshot_id "
        "WHERE e.screenshot_id != ? "
        "ORDER BY s.captured_at DESC "
        "LIMIT ?",
        (exclude_id, _CANDIDATE_LIMIT),
    )
    return list(await cursor.fetchall())
