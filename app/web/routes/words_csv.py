"""HTTP route for the corpus-wide top-words CSV export — v0.93.

``GET /export/words.csv?days=N&n=200`` streams a ``text/csv`` document
with the top ``n`` most frequent words across every screenshot's OCR
text plus every screenshot note created in the last ``days`` days.
Output columns (in this order):

    word     — the token, post-tokenisation (lower-cased, alphanumeric
               only).
    count    — exact occurrence count across the corpus window.
    percent  — share of the *post-filter* token total, formatted with
               two decimals (e.g. ``"3.42"``).  Sums of the column do
               not need to reach ``100.00`` exactly because we only
               emit the top ``n`` rows and the percentage is computed
               against the full filtered total, not the truncated set
               — that way a single CSV cell is comparable across
               different ``n`` values for the same window.

Tokenisation, lower-casing, and the STOPWORDS filter all reuse
:mod:`app.keywords` (v0.28) so the CSV is a strict superset of what
the keyword-cloud page already displays.  Min token length is hard-
coded to 4 — same default as :func:`app.keywords.top_keywords` — so a
caller toggling ``n`` between web and CSV gets the same vocabulary.

SQL is parametrised: the look-back ISO timestamp is bound, never
interpolated.  The route streams the entire body as a single
``StreamingResponse`` chunk (mirroring :mod:`app.web.routes.stats_csv`)
because the CSV is bounded by ``n`` rows (default 200, max 5000) — no
need for incremental writes.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.keywords import STOPWORDS, _tokenise
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.words_csv")

router = APIRouter(prefix="/export", tags=["words-csv"])

# Clamp windows mirror the rest of the ``/export/*.csv`` family.  The
# minimum-length default matches :func:`app.keywords.top_keywords` so
# the CSV vocabulary tracks the on-screen keyword cloud exactly.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 3650
_MIN_N: Final[int] = 1
_MAX_N: Final[int] = 5000
_DEFAULT_DAYS: Final[int] = 30
_DEFAULT_N: Final[int] = 200
_MIN_LENGTH: Final[int] = 4

_CSV_COLUMNS: Final[tuple[str, ...]] = ("word", "count", "percent")


async def _render_words_csv(*, days: int, top_n: int) -> str:
    """Count tokens across the corpus window and return the CSV body.

    Split out from the route so the CLI subcommand in :mod:`app.cli`
    can reuse the exact same SQL + tokenisation without going through
    FastAPI.  Pure async function — never touches the FastAPI request
    cycle, no globals beyond the structlog logger.

    Args:
        days: Look-back window in days (inclusive of "now").  Negative
            or zero values are rejected by the caller via the
            ``Query(..., ge=_MIN_DAYS)`` constraint, but we re-check
            here so the CLI path is also defensive.
        top_n: Maximum number of rows to emit, excluding the header.

    Returns:
        A complete CSV body (header + up to ``top_n`` data rows) as a
        string.  Empty corpora still produce a valid CSV with just the
        header — matches the behaviour of the other CSV exporters.
    """
    if days < _MIN_DAYS or top_n < _MIN_N:
        # Defensive — the FastAPI Query constraints already enforce
        # these bounds for HTTP callers, but the CLI path lands here
        # too and we'd rather emit a structured warning than a stack
        # trace.
        log.warning("words_csv.invalid_params", days=days, top_n=top_n)
        buffer_empty = io.StringIO(newline="")
        writer_empty = csv.writer(buffer_empty)
        writer_empty.writerow(_CSV_COLUMNS)
        return buffer_empty.getvalue()

    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_iso = iso(cutoff)

    counter: Counter[str] = Counter()
    ocr_chars = 0
    note_chars = 0
    total_tokens = 0

    async with get_connection() as conn:
        # Parametrised SQL: the cutoff is bound, never interpolated.
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE captured_at >= ? "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (cutoff_iso,),
        )
        async for row in cursor:
            text = str(row["ocr_text"])
            ocr_chars += len(text)
            for token in _tokenise(text):
                if len(token) < _MIN_LENGTH or token in STOPWORDS:
                    continue
                counter[token] += 1
                total_tokens += 1

        cursor = await conn.execute(
            "SELECT body FROM screenshot_notes WHERE created_at >= ?",
            (cutoff_iso,),
        )
        async for row in cursor:
            body = str(row["body"])
            note_chars += len(body)
            for token in _tokenise(body):
                if len(token) < _MIN_LENGTH or token in STOPWORDS:
                    continue
                counter[token] += 1
                total_tokens += 1

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)

    # Percentages divide by the full post-filter token total so a
    # single cell is comparable across different ``top_n`` values for
    # the same window — see module docstring.
    denom = total_tokens if total_tokens > 0 else 1
    rows_written = 0
    for word, count in counter.most_common(top_n):
        percent = (count / denom) * 100.0
        writer.writerow((word, count, f"{percent:.2f}"))
        rows_written += 1

    log.info(
        "words_csv.render.ok",
        days=days,
        top_n=top_n,
        unique_tokens=len(counter),
        total_tokens=total_tokens,
        ocr_chars=ocr_chars,
        note_chars=note_chars,
        rows=rows_written,
    )

    return buffer.getvalue()


@router.get("/words.csv", response_model=None)
async def export_words_csv_route(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
    n: int = Query(default=_DEFAULT_N, ge=_MIN_N, le=_MAX_N),
) -> StreamingResponse:
    """Stream the corpus-wide top-words CSV for the last ``days`` days."""
    try:
        body = await _render_words_csv(days=days, top_n=n)
    except Exception:
        log.exception("words_csv.route.failed", days=days, n=n)
        raise HTTPException(
            status_code=500,
            detail="words CSV export failed",
        ) from None

    payload = body.encode("utf-8")
    filename = f"persona-words-{days}d-top{n}.csv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("words_csv.route.ok", days=days, n=n, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
