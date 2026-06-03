"""Aggregate stats for the semantic-search index.

v0.80 ships a small read-only panel that answers four operator
questions in a single render:

* How many screenshots could in principle carry an embedding (OCR done,
  text long enough)?
* How many of those actually have a vector in ``screenshot_embeddings``?
* Which model produced the stored vectors — and does it match the
  ``PERSONA_EMBEDDINGS_MODEL`` the live process is configured with?
* When was the last full re-index performed?

The function is intentionally a thin, dict-returning surface that
mirrors :mod:`app.idle_stats` — the route layer wraps it for HTML and
JSON without further reshaping. All SQL is parametrised; no value the
caller hands us reaches the query string concatenated.

Why not derive ``embedded_pct`` server-side as a precomputed integer?
The dashboard wants a float (`12.3%`) and a JSON consumer might want
the raw ratio; we return the float once and let the template ``format``
it. Division-by-zero is the only edge case — we guard it explicitly so
the JSON payload is never ``NaN``.

The kv keys we honour are:

* ``embeddings_model`` — overrides ``settings.embeddings_model`` when
  present. Used so an operator can pin a specific model from the admin
  UI without touching env vars and a deploy.
* ``last_reindex_at`` — ISO timestamp of the last completed bulk
  re-index. Written by the re-index job once that lands; until then we
  return ``None`` and the template renders an em-dash.

The ``dimension`` value is the ``dim`` column from any one row in
``screenshot_embeddings``. We pick the most-recent row so a model
swap that changes vector width is reflected even before a full
re-index completes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.embeddings.stats")


class EmbeddingsStats(TypedDict):
    """Snapshot returned by :func:`compute_embeddings_stats`.

    * ``total_shots`` — rows in ``screenshots`` whose OCR completed and
      whose text is long enough that the worker would attempt to embed
      them (``length(ocr_text) >= embeddings_min_text_length``). This is
      the *denominator* for ``embedded_pct``; the raw ``COUNT(*)`` of
      the whole table would mix in OCR-pending rows the worker has not
      yet had a chance to consider and skew the percentage downward.
    * ``embedded_count`` — rows in ``screenshot_embeddings`` regardless
      of which model produced them. A mixed-model corpus reads as 100%
      embedded even when the live model has changed — re-index brings
      it back in line.
    * ``embedded_pct`` — ``embedded_count / total_shots * 100``, rounded
      to one decimal. ``0.0`` when ``total_shots == 0`` so the JSON
      payload never carries ``NaN`` / ``inf``.
    * ``last_reindex_at`` — value of the ``last_reindex_at`` kv row, or
      ``None`` if the kv row is missing. ISO-8601 string when present.
    * ``dimension`` — ``dim`` column of the freshest row in
      ``screenshot_embeddings``, or ``None`` when the table is empty.
    """

    total_shots: int
    embedded_count: int
    embedded_pct: float
    model: str
    last_reindex_at: str | None
    dimension: int | None


async def _read_kv(conn: aiosqlite.Connection, key: str) -> str | None:
    """Read a single ``kv_settings`` row, returning ``None`` when missing.

    Lives here rather than reusing :func:`app.storage.repository.get_kv`
    to keep this module's dependency footprint minimal — the repository
    module imports a number of domain types we don't need.
    """
    cursor = await conn.execute(
        "SELECT value FROM kv_settings WHERE key = ?",
        (key,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    value = row["value"]
    if value is None:
        return None
    return str(value)


async def _count_eligible_shots(
    conn: aiosqlite.Connection,
    min_text_length: int,
) -> int:
    """Rows the embeddings worker considers candidates for indexing.

    Mirrors the predicate in
    :func:`app.embeddings.storage.list_unindexed_screenshots` so the
    ratio we surface matches what the worker will actually catch up to.
    """
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE ocr_status = 'done' "
        "  AND ocr_text IS NOT NULL "
        "  AND length(ocr_text) >= ?",
        (min_text_length,),
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def _count_embedded(conn: aiosqlite.Connection) -> int:
    """Rows that actually carry an embedding BLOB."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshot_embeddings"
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def _read_dimension(conn: aiosqlite.Connection) -> int | None:
    """Pick the ``dim`` of the freshest row in ``screenshot_embeddings``.

    Picking by ``created_at`` instead of ``MAX(dim)`` matters when a
    model swap mid-deploy leaves two vector widths in the table — the
    freshest row is the one the live worker is writing and the value an
    operator wants to see.
    """
    cursor = await conn.execute(
        "SELECT dim FROM screenshot_embeddings "
        "ORDER BY created_at DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return int(row["dim"])


async def compute_embeddings_stats() -> EmbeddingsStats:
    """Compute the live embeddings dashboard snapshot.

    Single async DB transaction so all four counts come from the same
    point in time — a long-running re-index will not race the renderer
    and produce a > 100% ratio.
    """
    settings = get_settings()
    min_text_length = settings.embeddings_min_text_length

    async with get_connection() as conn:
        total_shots = await _count_eligible_shots(conn, min_text_length)
        embedded_count = await _count_embedded(conn)
        dimension = await _read_dimension(conn)
        kv_model = await _read_kv(conn, "embeddings_model")
        last_reindex_at = await _read_kv(conn, "last_reindex_at")

    model = kv_model if kv_model else settings.embeddings_model

    embedded_pct = (
        round(embedded_count / total_shots * 100.0, 1) if total_shots > 0 else 0.0
    )

    log.debug(
        "embeddings.stats.computed",
        total_shots=total_shots,
        embedded_count=embedded_count,
        embedded_pct=embedded_pct,
        model=model,
        dimension=dimension,
        last_reindex_at=last_reindex_at,
    )

    return EmbeddingsStats(
        total_shots=total_shots,
        embedded_count=embedded_count,
        embedded_pct=embedded_pct,
        model=model,
        last_reindex_at=last_reindex_at,
        dimension=dimension,
    )
