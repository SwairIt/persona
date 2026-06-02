"""OCR retry queue — find shots whose OCR result is empty or low-confidence.

The existing :mod:`app.storage.ocr_admin` flips ``skipped`` / ``failed`` rows
back to ``pending``. This module is its complement: it surfaces rows where
the OCR worker *succeeded* (``ocr_status = 'done'``) but the result is bad
enough to warrant another pass — typically empty text from a degenerate
crop, or a paragraph that the engine read with sub-50 average confidence.

The actual re-queue is just ``UPDATE screenshots SET ocr_status='pending'``;
the OCR worker (see :mod:`app.workers`) picks up pending rows on its next
sweep. We never delete the existing ``ocr_text`` or ``ocr_word`` rows here
— the worker overwrites them on the next successful pass — so a botched
retry leaves the prior result intact.

All SQL is parametrised; thresholds and limits are validated to keep the
admin route from issuing pathological queries.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr.retry")

# Cap on how many rows ``list_problem_shots`` will ever return / ``requeue``
# will ever touch in one call. The admin UI also enforces this on the
# ``requeue-all-shown`` button so a single click can never re-queue a
# six-figure backlog.
MAX_LIMIT: int = 1000

# Default average-confidence threshold below which a row is "low-conf".
# 50 matches the OCR overlay's red band in :mod:`app.web.routes.ocr_overlay`.
DEFAULT_MIN_CONF: int = 50


def _clamp_limit(limit: int) -> int:
    """Clamp ``limit`` into ``[1, MAX_LIMIT]``. Non-positive values become 1."""
    if limit < 1:
        return 1
    if limit > MAX_LIMIT:
        return MAX_LIMIT
    return limit


def _clamp_conf(min_conf: int) -> int:
    """Clamp the confidence threshold into ``[0, 100]``."""
    if min_conf < 0:
        return 0
    if min_conf > 100:
        return 100
    return min_conf


async def list_problem_shots(
    limit: int = 200,
    min_conf: int = DEFAULT_MIN_CONF,
    *,
    only_empty: bool = False,
    only_low: bool = False,
) -> list[dict[str, Any]]:
    """Return screenshots whose OCR pass produced an empty or low-confidence result.

    ``only_empty`` restricts the result to rows with ``ocr_text IS NULL`` or
    ``ocr_text = ''``. ``only_low`` restricts it to rows whose stored
    per-word confidence averages below ``min_conf`` (rows with no
    ``ocr_word`` rows are excluded from the low bucket because we have no
    signal to judge them).

    With both flags ``False`` (the default) the result is the union of the
    two buckets.

    Each row dict carries the fields the admin template needs to render a
    table line: ``id``, ``thumbnail_path``, ``app_name``, ``captured_at``,
    ``ocr_text_empty`` (bool), ``avg_conf`` (float | None) and ``reason``
    (``"empty"`` / ``"low_conf"``).

    Only rows where the worker actually finished (``ocr_status = 'done'``)
    are considered — pending rows are already queued and skipped / failed
    rows are the territory of :mod:`app.storage.ocr_admin`.
    """
    if only_empty and only_low:
        msg = "only_empty and only_low are mutually exclusive"
        raise ValueError(msg)

    capped_limit = _clamp_limit(limit)
    capped_conf = _clamp_conf(min_conf)

    # We always JOIN against the aggregated confidence so the template can
    # show ``avg_conf`` regardless of which bucket the row falls into.
    # Use LEFT JOIN to keep rows that have no ``ocr_word`` entries (typical
    # for empty OCR results — the worker stores zero words).
    base_query = (
        "SELECT s.id AS id, "
        "       s.thumbnail_path AS thumbnail_path, "
        "       s.app_name AS app_name, "
        "       s.window_title AS window_title, "
        "       s.captured_at AS captured_at, "
        "       s.ocr_text AS ocr_text, "
        "       w.avg_conf AS avg_conf, "
        "       w.word_count AS word_count "
        "FROM screenshots s "
        "LEFT JOIN ( "
        "    SELECT screenshot_id, "
        "           AVG(conf) AS avg_conf, "
        "           COUNT(*) AS word_count "
        "    FROM ocr_word "
        "    GROUP BY screenshot_id "
        ") w ON w.screenshot_id = s.id "
        "WHERE s.ocr_status = 'done' "
        "  AND s.thumbnail_path IS NOT NULL "
    )

    # ``empty_clause`` and ``low_clause`` are static SQL fragments —
    # parameters slot into the placeholders below. Never f-string user
    # input into either fragment.
    empty_clause = "(s.ocr_text IS NULL OR s.ocr_text = '')"
    low_clause = "(w.word_count IS NOT NULL AND w.word_count > 0 AND w.avg_conf < ?)"

    params: list[object] = []
    if only_empty:
        where_clause = f"AND {empty_clause}"
    elif only_low:
        where_clause = f"AND {low_clause}"
        params.append(capped_conf)
    else:
        where_clause = f"AND ({empty_clause} OR {low_clause})"
        params.append(capped_conf)

    query = base_query + where_clause + " ORDER BY s.captured_at DESC LIMIT ?"
    params.append(capped_limit)

    async with get_connection() as conn:
        cursor = await conn.execute(query, tuple(params))
        rows = await cursor.fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        ocr_text = row["ocr_text"]
        ocr_text_empty = ocr_text is None or str(ocr_text).strip() == ""
        avg_conf_raw = row["avg_conf"]
        avg_conf: float | None = None if avg_conf_raw is None else float(avg_conf_raw)
        word_count = 0 if row["word_count"] is None else int(row["word_count"])
        is_low = word_count > 0 and avg_conf is not None and avg_conf < float(capped_conf)
        reason = "empty" if ocr_text_empty else ("low_conf" if is_low else "empty")
        result.append(
            {
                "id": int(row["id"]),
                "thumbnail_path": (
                    None if row["thumbnail_path"] is None else str(row["thumbnail_path"])
                ),
                "app_name": (None if row["app_name"] is None else str(row["app_name"])),
                "window_title": (None if row["window_title"] is None else str(row["window_title"])),
                "captured_at": str(row["captured_at"]),
                "ocr_text_empty": ocr_text_empty,
                "avg_conf": avg_conf,
                "word_count": word_count,
                "reason": reason,
            }
        )

    log.info(
        "ocr.retry.list",
        rows=len(result),
        limit=capped_limit,
        min_conf=capped_conf,
        only_empty=only_empty,
        only_low=only_low,
    )
    return result


async def count_problem_shots(
    min_conf: int = DEFAULT_MIN_CONF,
    *,
    only_empty: bool = False,
    only_low: bool = False,
) -> int:
    """Count rows that would match :func:`list_problem_shots` (ignoring ``limit``).

    Used by the admin template to render the "Re-queue all (max 1000)"
    button's caption without materialising every row.
    """
    if only_empty and only_low:
        msg = "only_empty and only_low are mutually exclusive"
        raise ValueError(msg)

    capped_conf = _clamp_conf(min_conf)

    base_query = (
        "SELECT COUNT(*) AS n "
        "FROM screenshots s "
        "LEFT JOIN ( "
        "    SELECT screenshot_id, "
        "           AVG(conf) AS avg_conf, "
        "           COUNT(*) AS word_count "
        "    FROM ocr_word "
        "    GROUP BY screenshot_id "
        ") w ON w.screenshot_id = s.id "
        "WHERE s.ocr_status = 'done' "
        "  AND s.thumbnail_path IS NOT NULL "
    )

    empty_clause = "(s.ocr_text IS NULL OR s.ocr_text = '')"
    low_clause = "(w.word_count IS NOT NULL AND w.word_count > 0 AND w.avg_conf < ?)"

    params: list[object] = []
    if only_empty:
        where_clause = f"AND {empty_clause}"
    elif only_low:
        where_clause = f"AND {low_clause}"
        params.append(capped_conf)
    else:
        where_clause = f"AND ({empty_clause} OR {low_clause})"
        params.append(capped_conf)

    query = base_query + where_clause

    async with get_connection() as conn:
        cursor = await conn.execute(query, tuple(params))
        row = await cursor.fetchone()

    return 0 if row is None else int(row["n"])


async def requeue_shots(ids: list[int]) -> int:
    """Re-queue the given screenshot ids — sets ``ocr_status = 'pending'``.

    Rows without a ``thumbnail_path`` are skipped (the OCR worker has no
    image to read). Rows that are currently ``pending`` are touched
    harmlessly. Returns the count of rows actually updated.

    Ids are clamped to ``MAX_LIMIT`` to keep a single call from issuing a
    runaway UPDATE — the route layer also enforces this.
    """
    if not ids:
        return 0

    # Drop non-ints defensively (form data starts life as ``str``).
    clean_ids: list[int] = []
    for value in ids:
        try:
            clean_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    if not clean_ids:
        return 0

    if len(clean_ids) > MAX_LIMIT:
        log.warning(
            "ocr.retry.requeue.truncated",
            requested=len(clean_ids),
            kept=MAX_LIMIT,
        )
        clean_ids = clean_ids[:MAX_LIMIT]

    placeholders = ",".join("?" for _ in clean_ids)
    # ``placeholders`` is "?,?,?..." — built from the *length* of clean_ids
    # only, never from user input. The ids themselves are bound below.
    query = (
        "UPDATE screenshots SET ocr_status = 'pending' "  # noqa: S608 — static "?" tokens
        f"WHERE id IN ({placeholders}) AND thumbnail_path IS NOT NULL"
    )

    async with get_connection() as conn:
        cursor = await conn.execute(query, tuple(clean_ids))
        await conn.commit()
        affected = cursor.rowcount or 0

    log.info(
        "ocr.retry.requeue",
        requested=len(clean_ids),
        affected=affected,
    )
    return affected


async def requeue_matching(
    min_conf: int = DEFAULT_MIN_CONF,
    *,
    only_empty: bool = False,
    only_low: bool = False,
    cap: int = MAX_LIMIT,
) -> int:
    """Re-queue every row that matches the current filter, capped at ``cap``.

    Implemented as a SELECT-then-UPDATE rather than a single UPDATE-FROM so
    the same predicate logic lives in one place (see
    :func:`list_problem_shots`) and we can apply ``cap`` deterministically.
    """
    if only_empty and only_low:
        msg = "only_empty and only_low are mutually exclusive"
        raise ValueError(msg)

    capped_cap = _clamp_limit(cap)
    rows = await list_problem_shots(
        limit=capped_cap,
        min_conf=min_conf,
        only_empty=only_empty,
        only_low=only_low,
    )
    ids = [row["id"] for row in rows]
    affected = await requeue_shots(ids)
    log.info(
        "ocr.retry.requeue_matching",
        cap=capped_cap,
        matched=len(ids),
        affected=affected,
        only_empty=only_empty,
        only_low=only_low,
        min_conf=min_conf,
    )
    return affected
