"""OCR force re-index — wipe OCR state and let the worker re-OCR matching shots.

This module is the heavy hammer next to :mod:`app.storage.ocr_admin` (which
only flips ``skipped`` / ``failed`` rows back) and :mod:`app.ocr_retry`
(which targets ``done`` rows with bad results). ``wipe_and_requeue``
indiscriminately resets ``ocr_status`` to ``pending`` for every shot in
the selected window — used by the CLI after the operator swaps Tesseract
language packs, upgrades the binary, or otherwise wants the entire corpus
re-read from scratch.

Only rows with a non-NULL ``thumbnail_path`` are touched, mirroring the
existing admin helpers: without an image on disk the OCR worker has
nothing to do and would just bounce the row back into the ``skipped``
bucket on its next sweep.

Filtering:

* ``app_filter`` — optional case-sensitive exact match against
  ``screenshots.app_name`` (matches the column verbatim, no LIKE).
* ``days_back`` — optional lookback window. ``days_back=7`` keeps rows
  captured within the last seven days; ``None`` means "no time bound".

Both filters compose with AND. With neither set the call resets every
shot in the database. All SQL is parametrised; no user input is ever
interpolated into the query string.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.ocr.reindex")


def _build_where(
    app_filter: str | None,
    days_back: int | None,
) -> tuple[str, tuple[object, ...]]:
    """Compose the WHERE clause + parameter tuple shared by count + update.

    Always includes ``thumbnail_path IS NOT NULL`` because the OCR worker
    has nothing to read without an image on disk. ``app_filter`` and
    ``days_back`` AND on top of that base predicate.
    """
    clauses: list[str] = ["thumbnail_path IS NOT NULL"]
    params: list[object] = []

    if app_filter is not None:
        clauses.append("app_name = ?")
        params.append(app_filter)

    if days_back is not None:
        cutoff = datetime.now(tz=UTC) - timedelta(days=days_back)
        clauses.append("captured_at >= ?")
        params.append(iso(cutoff))

    where = " AND ".join(clauses)
    return where, tuple(params)


async def count_candidates(
    app_filter: str | None = None,
    days_back: int | None = None,
) -> int:
    """Return how many shots :func:`wipe_and_requeue` would reset.

    Used by the CLI dry-run path so the operator sees the blast radius
    before passing ``--confirm``.
    """
    where, params = _build_where(app_filter, days_back)
    query = f"SELECT COUNT(*) AS n FROM screenshots WHERE {where}"  # noqa: S608 — static "?" tokens

    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()

    count = 0 if row is None else int(row["n"])
    log.info(
        "ocr.reindex.count",
        app_filter=app_filter,
        days_back=days_back,
        count=count,
    )
    return count


async def wipe_and_requeue(
    app_filter: str | None = None,
    days_back: int | None = None,
) -> int:
    """Reset matching shots back to ``ocr_status='pending'`` for re-OCR.

    The OCR worker picks ``pending`` rows up on its next sweep and
    overwrites the prior ``ocr_text`` / ``ocr_word`` rows on success, so
    we deliberately do *not* delete the old result here — a crashed
    worker leaves the previous OCR text visible until it succeeds again.

    Returns the number of rows actually updated. ``app_filter`` is an
    exact-match against ``app_name``; ``days_back`` keeps only rows
    captured within the last N days. Passing both is an AND.
    """
    where, params = _build_where(app_filter, days_back)
    query = (
        "UPDATE screenshots SET ocr_status = 'pending' "  # noqa: S608 — static "?" tokens
        f"WHERE {where}"
    )

    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        await conn.commit()
        affected = cursor.rowcount or 0

    log.info(
        "ocr.reindex.wipe",
        app_filter=app_filter,
        days_back=days_back,
        affected=affected,
    )
    return affected


__all__ = ["count_candidates", "wipe_and_requeue"]
