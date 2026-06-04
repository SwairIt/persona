"""Aggregate stats over the ``audio_segment`` table.

v1.11 feature 3/3, route 5 of 5. Pairs an HTML dashboard with a JSON
sibling — same numbers, same window math, so a future widget or
CLI can consume :file:`/api/audio-stats.json` without re-deriving the
totals.

Surface
-------

* ``GET /stats/audio``         — Tailwind page (audio_stats.html).
* ``GET /api/audio-stats.json`` — machine-readable JSON.

Five buckets are computed:

1. ``today_seconds``     — total recorded duration on the current
   local day, in seconds.
2. ``week_seconds``      — same for the trailing 7 calendar days
   (inclusive of today).
3. ``lifetime_seconds``  — the whole-table sum.
4. ``disk_bytes``        — sum of ``size_bytes`` for rows whose
   ``path`` is still populated (after retention purge the row keeps
   the historic size_bytes value, but those bytes no longer live on
   disk — we exclude them so the operator sees real disk pressure,
   not a historic high-water mark).
5. ``segment_count``     — count of rows on disk (i.e. with a
   populated ``path``) **and** the lifetime row count, so the
   template can show both "5 412 segments captured ever, 312 still
   on disk".
6. ``top_words``         — keyword frequency over every populated
   transcript. Reuses :data:`app.keywords.STOPWORDS` and the same
   tokeniser so the audio-side wordcloud aligns with what the OCR
   side already considers signal vs noise.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main` — the task spec forbids touching
``main.py``. Wire it up with::

    from app.web.routes import audio_stats as audio_stats_routes
    app.include_router(audio_stats_routes.router)
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.keywords import STOPWORDS
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.audio.web")

router = APIRouter(tags=["audio-stats"])

# Drop tokens shorter than this after cleaning. Mirrors the default in
# :func:`app.keywords.top_keywords` (4) so the audio-side wordcloud
# excludes the same junk the OCR-side one already filters out.
_MIN_TOKEN_LENGTH: Final[int] = 4

# Cap on the number of distinct keywords returned. 30 is what the OCR
# side surfaces — keeps the two stat pages visually consistent.
_TOP_N: Final[int] = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_local() -> date:
    """Local-date "today" — matches the wall clock the operator sees."""
    return datetime.now().astimezone().date()


def _tokenise(text: str) -> list[str]:
    """Split ``text`` on whitespace, strip non-alphanumerics, lowercase.

    Duplicates the body of :func:`app.keywords._tokenise` rather than
    importing the private symbol — the keywords module is OCR-tied
    (it knows about ``screenshots.ocr_text`` columns) and we'd rather
    not couple the audio stats to that internal layout.
    """
    tokens: list[str] = []
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append(cleaned.lower())
    return tokens


def _top_words_from_transcripts(transcripts: list[str]) -> list[dict[str, int | str]]:
    """Return the top :data:`_TOP_N` keywords from a list of transcripts.

    Reuses :data:`app.keywords.STOPWORDS` so an English / Russian
    function word filtered out of OCR text is filtered out of the
    audio side too. Tokens shorter than :data:`_MIN_TOKEN_LENGTH`
    drop out.
    """
    counter: Counter[str] = Counter()
    for transcript in transcripts:
        if not transcript:
            continue
        for token in _tokenise(transcript):
            if len(token) < _MIN_TOKEN_LENGTH or token in STOPWORDS:
                continue
            counter[token] += 1
    return [{"word": word, "count": count} for word, count in counter.most_common(_TOP_N)]


async def _collect_audio_stats() -> dict[str, Any]:
    """Compute every audio-stats bucket from the ``audio_segment`` table.

    Built around four parametrised queries instead of one big GROUP BY
    so the per-bucket intent is readable in the source. The today /
    week numbers use SQLite's ``date(captured_at)`` against a Python-
    computed ISO day string so the timezone behaviour matches the
    rest of the day-keyed pages (notes_timeline, day_scrubber, ...).
    """
    today = _today_local()
    week_start = today - timedelta(days=6)  # inclusive of today → 7 days
    today_str = today.strftime("%Y-%m-%d")
    week_start_str = week_start.strftime("%Y-%m-%d")

    transcripts: list[str] = []

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(duration_seconds), 0.0) AS total_seconds
              FROM audio_segment
             WHERE date(captured_at) = ?
            """,
            (today_str,),
        )
        row = await cursor.fetchone()
        today_seconds = float(row["total_seconds"]) if row is not None else 0.0

        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(duration_seconds), 0.0) AS total_seconds
              FROM audio_segment
             WHERE date(captured_at) >= ?
            """,
            (week_start_str,),
        )
        row = await cursor.fetchone()
        week_seconds = float(row["total_seconds"]) if row is not None else 0.0

        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(duration_seconds), 0.0) AS total_seconds,
                   COUNT(*) AS total_count
              FROM audio_segment
            """
        )
        row = await cursor.fetchone()
        lifetime_seconds = float(row["total_seconds"]) if row is not None else 0.0
        lifetime_count = int(row["total_count"]) if row is not None else 0

        # Only sum sizes for rows that still have an on-disk file.
        # Retention reaped rows keep the historic ``size_bytes`` value
        # but the bytes are gone — including them would lie about
        # current disk pressure.
        cursor = await conn.execute(
            """
            SELECT COALESCE(SUM(size_bytes), 0) AS total_bytes,
                   COUNT(*) AS on_disk_count
              FROM audio_segment
             WHERE path IS NOT NULL AND path != ''
            """
        )
        row = await cursor.fetchone()
        disk_bytes = int(row["total_bytes"]) if row is not None else 0
        on_disk_count = int(row["on_disk_count"]) if row is not None else 0

        cursor = await conn.execute(
            """
            SELECT transcript
              FROM audio_segment
             WHERE transcript IS NOT NULL AND transcript != ''
            """
        )
        async for trow in cursor:
            transcripts.append(str(trow["transcript"]))

    top_words = _top_words_from_transcripts(transcripts)

    return {
        "today_seconds": today_seconds,
        "today_minutes": today_seconds / 60.0,
        "week_seconds": week_seconds,
        "week_minutes": week_seconds / 60.0,
        "lifetime_seconds": lifetime_seconds,
        "lifetime_minutes": lifetime_seconds / 60.0,
        "disk_bytes": disk_bytes,
        "disk_mb": disk_bytes / (1024.0 * 1024.0),
        "lifetime_count": lifetime_count,
        "on_disk_count": on_disk_count,
        "top_words": top_words,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/stats/audio", response_class=HTMLResponse)
async def audio_stats_page(request: Request) -> HTMLResponse:
    """Render the audio stats dashboard."""
    payload = await _collect_audio_stats()
    log.info(
        "audio.stats.page",
        today_seconds=payload["today_seconds"],
        week_seconds=payload["week_seconds"],
        lifetime_seconds=payload["lifetime_seconds"],
        disk_bytes=payload["disk_bytes"],
        lifetime_count=payload["lifetime_count"],
        on_disk_count=payload["on_disk_count"],
        top_words=len(payload["top_words"]),
    )
    return templates.TemplateResponse(
        request,
        "audio_stats.html",
        {
            "title": "Audio stats",
            "active_nav": "stats",
            **payload,
        },
    )


@router.get("/api/audio-stats.json", response_class=JSONResponse)
async def audio_stats_json() -> JSONResponse:
    """Machine-readable companion to :func:`audio_stats_page`."""
    payload = await _collect_audio_stats()
    log.info(
        "audio.stats.json",
        today_seconds=payload["today_seconds"],
        week_seconds=payload["week_seconds"],
        lifetime_seconds=payload["lifetime_seconds"],
        disk_bytes=payload["disk_bytes"],
    )
    return JSONResponse(payload)


__all__ = ["router"]
