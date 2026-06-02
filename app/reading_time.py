"""Per-day reading-time estimate — words across OCR text and notes.

v0.48 feature 2/3. Counts every whitespace-separated token in the day's
OCR'd screenshots plus the day's plaintext notes, then converts the
grand total into a "minutes spent reading" figure at the standard
adult average pace of **250 words per minute**.

Two note sources are folded in:

* ``screenshot_notes`` — free-text annotations pinned to a screenshot
  row (created in ``002_notes.sql``). These never have an encryption
  flag, so every row counts.
* ``notes`` — standalone markdown notes from the inbox watch folder
  (created in ``039_inbox_notes.sql``, encryption added in
  ``045_encrypted_notes.sql``). Rows with ``encrypted = 1`` are
  **deliberately skipped** — the ciphertext is opaque by design, and
  treating ``LENGTH(ciphertext)`` as a word count would either lie or
  leak structural metadata. Skipping is the safe default.

The OCR side is bucketed per app so the response can render a "where
those words came from" bar chart without a second round-trip. Notes are
*not* bucketed per app because the ``notes`` table has no app column —
they live outside the screenshots stream.

Word count is the dead-simple :py:meth:`str.split` length: it matches
how most "words per minute" benchmarks measure text and it sidesteps
the language-detection rabbit hole that a smarter tokeniser would
demand (the screenshots+notes corpus mixes English, Russian and code).

Returns a plain ``dict`` so the FastAPI/JSON layer can hand it back
without any pydantic mapping; the shape is locked into a TypedDict so
mypy can see the field names at every call site.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.reading_time")

# Average adult reading pace used for the headline minutes figure.
# Picked to match the public "average reader = 200-300 wpm" range that
# every reading-time browser extension cites; 250 is the round middle.
DEFAULT_WPM: Final[int] = 250


class AppWords(TypedDict):
    """Per-app OCR-word bucket. ``app_name`` is the foreground process."""

    app_name: str
    words: int


class ReadingTimeResult(TypedDict):
    """Full payload for a single day."""

    day: str
    total_words_ocr: int
    total_words_notes: int
    total_words: int
    wpm: int
    minutes_at_250wpm: int
    by_app: list[AppWords]


def _count_words(text: str | None) -> int:
    """Return the number of whitespace-separated tokens in ``text``.

    ``text.split()`` (no separator argument) collapses runs of any
    whitespace and ignores leading/trailing whitespace, which is what
    every "words per minute" benchmark measures.  An empty string or
    ``None`` returns ``0`` without a branchy fast path — the call into
    ``str.split`` is cheap and keeps the function total-on-strings.
    """
    if not text:
        return 0
    return len(text.split())


def _parse_day_or_today(day_iso: str) -> date:
    """Parse a ``YYYY-MM-DD`` day string; fall back to local today.

    Same forgiving behaviour as :mod:`app.web.routes.notes_timeline` —
    a bad ``day`` query parameter should land the user on a sensible
    page rather than 400-ing.
    """
    try:
        return date.fromisoformat(day_iso)
    except ValueError:
        log.warning("reading_time.bad_day_iso", day_iso=day_iso)
        return datetime.now().astimezone().date()


async def reading_time_for_day(
    day_iso: str,
    wpm: int = DEFAULT_WPM,
) -> ReadingTimeResult:
    """Compute the per-day reading-time payload.

    Args:
        day_iso: ``YYYY-MM-DD`` calendar day to count against (matched
            via SQLite's ``date(captured_at) = ?`` so it lines up with
            every other day-scoped view in the app).
        wpm: Words-per-minute conversion rate. Defaults to
            :data:`DEFAULT_WPM` (250). The argument is exposed so a
            future settings page can let the user override it without
            touching this module's call sites; passing ``<= 0`` is
            treated as "use the default" rather than crashing on the
            zero-division.

    Returns:
        :class:`ReadingTimeResult` — a JSON-ready ``dict`` carrying the
        OCR / notes word totals, the converted minutes figure, and the
        per-app OCR breakdown sorted by ``words`` descending.

    Encrypted ``notes`` rows are excluded by the SQL ``WHERE`` clause
    (``encrypted = 0``), keeping the count honest without ever loading
    the ciphertext blob.
    """
    target = _parse_day_or_today(day_iso)
    effective_wpm = wpm if wpm > 0 else DEFAULT_WPM
    day_str = target.isoformat()

    by_app: dict[str, int] = {}
    total_words_ocr = 0
    total_words_notes = 0
    rows_ocr = 0
    rows_screenshot_notes = 0
    rows_inbox_notes = 0

    async with get_connection() as conn:
        # ----- OCR text per screenshot, bucketed by foreground app ----
        cursor = await conn.execute(
            "SELECT app_name, ocr_text FROM screenshots "
            "WHERE date(captured_at) = ? "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (day_str,),
        )
        async for row in cursor:
            rows_ocr += 1
            text = str(row["ocr_text"])
            count = _count_words(text)
            if count == 0:
                continue
            total_words_ocr += count
            app_raw = row["app_name"]
            app_name = str(app_raw) if app_raw is not None else "(unknown)"
            by_app[app_name] = by_app.get(app_name, 0) + count

        # ----- screenshot_notes (no encryption flag exists for these) -
        cursor = await conn.execute(
            "SELECT body FROM screenshot_notes WHERE date(created_at) = ?",
            (day_str,),
        )
        async for row in cursor:
            rows_screenshot_notes += 1
            total_words_notes += _count_words(str(row["body"]))

        # ----- inbox ``notes``, encrypted rows skipped at the SQL layer
        cursor = await conn.execute(
            "SELECT body FROM notes "
            "WHERE date(created_at) = ? AND encrypted = 0",
            (day_str,),
        )
        async for row in cursor:
            rows_inbox_notes += 1
            total_words_notes += _count_words(str(row["body"]))

    by_app_items: list[AppWords] = [
        AppWords(app_name=name, words=words)
        for name, words in by_app.items()
    ]
    by_app_items.sort(key=lambda item: (item["words"], item["app_name"]), reverse=True)

    total_words = total_words_ocr + total_words_notes
    # Round to the nearest whole minute. Headline figure should never be
    # ``0`` when the user actually has *some* text on the day, so any
    # non-zero total clamps to at least 1 minute.
    minutes = round(total_words / effective_wpm) if total_words else 0
    if total_words > 0 and minutes < 1:
        minutes = 1

    result: ReadingTimeResult = {
        "day": day_str,
        "total_words_ocr": total_words_ocr,
        "total_words_notes": total_words_notes,
        "total_words": total_words,
        "wpm": effective_wpm,
        "minutes_at_250wpm": minutes,
        "by_app": by_app_items,
    }

    log.info(
        "reading_time.computed",
        day=day_str,
        total_words_ocr=total_words_ocr,
        total_words_notes=total_words_notes,
        total_words=total_words,
        wpm=effective_wpm,
        minutes=minutes,
        apps=len(by_app_items),
        rows_ocr=rows_ocr,
        rows_screenshot_notes=rows_screenshot_notes,
        rows_inbox_notes=rows_inbox_notes,
    )
    return result


__all__ = [
    "DEFAULT_WPM",
    "AppWords",
    "ReadingTimeResult",
    "reading_time_for_day",
]
