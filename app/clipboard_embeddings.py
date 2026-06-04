"""Semantic embedding helpers for the opt-in clipboard history.

Persona v1.18 feature. The clipboard worker
(:mod:`app.workers.clipboard_worker`) writes raw text into
``clipboard_event`` row-by-row; this module adds a *separate* backfill
helper that turns each ``text`` value into a fastembed vector and
stores it inline in the new ``embedding_blob`` BLOB column. The
worker itself is intentionally not touched — embedding is CPU-heavy
and bursts of clipboard activity must keep latency low — so this
module exposes a standalone :func:`backfill_clipboard_embeddings`
that a future scheduler can call manually in small batches.

The companion :func:`search_clipboard_semantic` powers the
``/clipboard/semantic`` route: embed the user's query once, brute-
force cosine against every stored vector, return the top *N* hits
shaped for the HTML / JSON renderers.

Graceful degrade is the rule: if the optional ``fastembed`` /
``numpy`` extras are missing the helpers log an info line and return
an empty result, never raising into the route layer. Mirrors the
defensive pattern used by :mod:`app.semantic_similar`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from app.embeddings.storage import decode_vector, encode_vector
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.clipboard_embeddings")

# Snippet length used by :func:`search_clipboard_semantic` for the
# returned ``content_text`` preview. Matches the legacy LIKE-search
# preview ceiling on the /clipboard page so the two surfaces feel
# consistent.
_PREVIEW_CHARS: Final[int] = 200

# Floor for "this hit is actually related" — anything below this is
# semantic noise and silently dropped. Matches the floor used by
# :func:`app.embeddings.search.semantic_search` so the screenshot
# search and the clipboard search share the same notion of "related".
_MIN_SIMILARITY: Final[float] = 0.15

# Hard cap on the candidate pool we score in a single query. Cosine
# over a 384-dim float32 vector is microseconds in numpy, but pulling
# every row from a 100k-snippet clipboard history still costs IO and
# a Python list per row. The cap keeps the worst case bounded; future
# work can swap this for an approximate index.
_CANDIDATE_LIMIT: Final[int] = 5000


def _try_import_numpy() -> Any | None:
    """Return the ``numpy`` module if importable, else ``None``.

    Kept local so a missing optional dep never crashes module import.
    Mirrors the pattern in :mod:`app.semantic_similar`.
    """
    try:
        import numpy as np  # noqa: PLC0415 — optional dep, must not crash import
    except ImportError:
        return None
    return np


def _try_embed_query(query: str) -> list[float] | None:
    """Embed ``query`` through the project's e5 model.

    Returns ``None`` (and logs ``info``) when either the fastembed
    extra or the model itself is unavailable — keeping the failure
    path identical to a "no hits" response so the route layer never
    has to special-case the missing-deps branch.
    """
    try:
        from app.embeddings import (  # noqa: PLC0415 — optional dep guard
            EmbeddingsNotAvailable,
            embed_query,
        )
    except ImportError:
        log.info("clipboard_embeddings.fastembed_missing")
        return None
    try:
        return embed_query(query)
    except EmbeddingsNotAvailable as exc:
        log.info("clipboard_embeddings.disabled", reason=str(exc))
        return None
    except ValueError:
        # ``embed_query`` raises on an empty / whitespace-only query;
        # the route layer guards against that already, so this is
        # defence-in-depth.
        return None


def _try_embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch-embed ``texts`` through the project's e5 model.

    Returns ``None`` (and logs ``info``) when the fastembed extra is
    missing or the model is disabled — the caller treats that as "no
    rows embedded" and returns the standard skipped/embedded counter
    dict.
    """
    if not texts:
        return []
    try:
        from app.embeddings import (  # noqa: PLC0415 — optional dep guard
            EmbeddingsNotAvailable,
            embed_texts,
        )
    except ImportError:
        log.info("clipboard_embeddings.fastembed_missing")
        return None
    try:
        return embed_texts(texts)
    except EmbeddingsNotAvailable as exc:
        log.info("clipboard_embeddings.disabled", reason=str(exc))
        return None


async def _fetch_unembedded_rows(
    conn: aiosqlite.Connection,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return clipboard rows still missing an embedding, oldest first.

    Filters out empty / whitespace-only ``text`` so the embedder is
    never handed a row it would reject anyway. Ordering by ``id ASC``
    is deterministic — re-runs of a partial backfill resume from the
    same place rather than re-scoring the newest material first.
    """
    cursor = await conn.execute(
        "SELECT id, text FROM clipboard_event "
        "WHERE embedding_blob IS NULL "
        "  AND text IS NOT NULL "
        "  AND length(trim(text)) > 0 "
        "ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    return [{"id": int(row["id"]), "text": str(row["text"])} for row in rows]


async def backfill_clipboard_embeddings(batch_limit: int = 32) -> dict[str, int]:
    """Embed up to ``batch_limit`` clipboard rows whose vector is NULL.

    Returns a small counter dict::

        {
            "rows_embedded": int,     # rows we successfully wrote a vector for
            "skipped_missing_text": int,  # rows skipped because text was empty/whitespace
        }

    ``rows_embedded`` is ``0`` (and ``skipped_missing_text`` is ``0``)
    when the fastembed dependency is missing or embeddings are
    globally disabled — the call is a silent no-op, mirroring the
    graceful-degrade contract used elsewhere in the project.

    The function never touches the clipboard worker: it only ever
    UPDATEs rows that already exist. The caller (a future scheduler /
    admin endpoint) is responsible for picking a sensible cadence.
    """
    if batch_limit <= 0:
        return {"rows_embedded": 0, "skipped_missing_text": 0}

    async with get_connection() as conn:
        # Pre-filter on the SQL side so the "skipped" counter only
        # ever reflects rows that genuinely had no text to embed —
        # rows with valid text but a failed embed never show up here
        # because we bail out before the SELECT.
        candidates = await _fetch_unembedded_rows(conn, limit=batch_limit)

    if not candidates:
        log.debug("clipboard_embeddings.backfill.empty", batch_limit=batch_limit)
        return {"rows_embedded": 0, "skipped_missing_text": 0}

    # Defence-in-depth: ``_fetch_unembedded_rows`` already drops
    # whitespace-only text, but a future SQL tweak might let one
    # through. Separate the two streams here so the returned counter
    # is always accurate.
    embeddable: list[dict[str, Any]] = []
    skipped_missing_text = 0
    for row in candidates:
        if row["text"].strip():
            embeddable.append(row)
        else:
            skipped_missing_text += 1

    if not embeddable:
        log.info(
            "clipboard_embeddings.backfill.all_skipped",
            batch_limit=batch_limit,
            skipped_missing_text=skipped_missing_text,
        )
        return {"rows_embedded": 0, "skipped_missing_text": skipped_missing_text}

    vectors = _try_embed_texts([row["text"] for row in embeddable])
    if vectors is None:
        return {"rows_embedded": 0, "skipped_missing_text": skipped_missing_text}

    rows_embedded = 0
    async with get_connection() as conn:
        for row, vec in zip(embeddable, vectors, strict=True):
            blob = encode_vector(vec)
            await conn.execute(
                "UPDATE clipboard_event SET embedding_blob = ? WHERE id = ?",
                (blob, row["id"]),
            )
            rows_embedded += 1
        await conn.commit()

    log.info(
        "clipboard_embeddings.backfill.done",
        batch_limit=batch_limit,
        rows_embedded=rows_embedded,
        skipped_missing_text=skipped_missing_text,
    )
    return {
        "rows_embedded": rows_embedded,
        "skipped_missing_text": skipped_missing_text,
    }


async def _fetch_embedded_rows(
    conn: aiosqlite.Connection,
) -> list[dict[str, Any]]:
    """Pull every clipboard row that already has an embedding.

    Capped at :data:`_CANDIDATE_LIMIT` newest-first so the scoring
    loop stays bounded on a runaway history. Returns dicts shaped for
    :func:`search_clipboard_semantic`'s scoring loop.
    """
    cursor = await conn.execute(
        "SELECT id, text, captured_at, embedding_blob FROM clipboard_event "
        "WHERE embedding_blob IS NOT NULL "
        "ORDER BY captured_at DESC, id DESC "
        "LIMIT ?",
        (_CANDIDATE_LIMIT,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "text": str(row["text"]),
            "captured_at": str(row["captured_at"]),
            "embedding_blob": bytes(row["embedding_blob"]),
        }
        for row in rows
    ]


def _make_snippet(text: str) -> str:
    """First :data:`_PREVIEW_CHARS` of the row, with an ellipsis on cut."""
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + "…"


async def search_clipboard_semantic(
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` clipboard rows semantically closest to ``query``.

    Each hit is shaped as::

        {
            "id": int,
            "content_text": str,   # the full original text (un-truncated)
            "snippet": str,        # first 200 chars + ellipsis if cut
            "created_at": str,     # ISO timestamp from ``captured_at``
            "similarity": float,   # cosine, rounded to 4 decimals
        }

    Returns ``[]`` when:

    * ``query`` is empty / whitespace-only
    * ``limit <= 0``
    * the fastembed or numpy optional deps are missing
    * embeddings are globally disabled
    * no row clears the :data:`_MIN_SIMILARITY` floor

    Never raises — the route layer turns an empty list into a friendly
    "no matches" UI state.
    """
    # PLR0911 noqa: the multiple early-returns are all the same shape
    # ("graceful degrade to empty list, log why") and folding them into
    # a single sentinel would obscure each distinct failure mode.
    if limit <= 0 or not query.strip():
        return []

    np = _try_import_numpy()
    if np is None:
        log.info("clipboard_embeddings.numpy_missing")
        return []

    query_vec = _try_embed_query(query)
    if query_vec is None:
        return []

    async with get_connection() as conn:
        rows = await _fetch_embedded_rows(conn)

    if not rows:
        log.debug("clipboard_embeddings.search.no_candidates")
        return []

    query_arr = np.asarray(query_vec, dtype=np.float32)
    query_norm = float(np.linalg.norm(query_arr))
    if query_norm == 0.0:
        log.debug("clipboard_embeddings.search.zero_norm_query")
        return []

    scored: list[dict[str, Any]] = []
    for row in rows:
        try:
            cand_vector = decode_vector(row["embedding_blob"])
        except (ValueError, TypeError):
            # Corrupt BLOB — skip rather than 500 the search.
            continue
        if len(cand_vector) != len(query_vec):
            # Embedding-model dim drift (operator changed
            # PERSONA_EMBEDDINGS_MODEL). Skip rather than score
            # against an incompatible space.
            continue
        cand_arr = np.asarray(cand_vector, dtype=np.float32)
        cand_norm = float(np.linalg.norm(cand_arr))
        if cand_norm == 0.0:
            continue
        similarity = float(np.dot(query_arr, cand_arr) / (query_norm * cand_norm))
        if similarity < _MIN_SIMILARITY:
            continue
        text = row["text"]
        scored.append(
            {
                "id": row["id"],
                "content_text": text,
                "snippet": _make_snippet(text),
                "created_at": row["captured_at"],
                "similarity": round(similarity, 4),
            }
        )

    scored.sort(key=lambda hit: hit["similarity"], reverse=True)
    top = scored[:limit]

    log.debug(
        "clipboard_embeddings.search.done",
        scanned=len(rows),
        kept=len(scored),
        returned=len(top),
    )
    return top
