"""Multi-day diff stats — compare two calendar days side-by-side.

Persona v0.71. Sister to :mod:`app.keywords` (single-window tag cloud) and
:mod:`app.ocr_diff` (per-screenshot text diff): this module rolls the whole
day's activity up into four overlapping sets — apps, tags, OCR keywords —
and returns the symmetric differences between two days.

The result is a plain :class:`MultiDayDiff` dataclass, exposed to FastAPI as
a dict via :meth:`MultiDayDiff.as_dict`. The route layer (HTML page +
``/api/diff-days.json``) is a thin shell on top of :func:`compare_days`.

Implementation notes:
    * Every SQL query is parametrised — the only "interpolated" values are
      the two day strings, and we validate them through :func:`_parse_day`
      before they ever touch the DB. SQLite ``DATE(captured_at) = ?``
      compares the leading ``YYYY-MM-DD`` slice of the ISO timestamp.
    * Keyword extraction reuses the public :data:`app.keywords.STOPWORDS`
      set and the same Unicode-aware tokeniser so the two pages stay
      consistent.
    * The top-keyword *delta* is computed by frequency difference (day_a
      minus day_b for ``top_gone_keywords``, and the reverse for
      ``top_new_keywords``) — that mirrors the user-facing question
      "what did I read / type on B that wasn't there on A?" instead of
      a naive set-difference that would drop high-volume shared words.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, TypedDict

import aiosqlite

from app.keywords import STOPWORDS
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.multi_day_diff")


# Cap the per-side keyword list so a runaway OCR day (10k+ unique tokens)
# can't bloat the JSON / HTML payload past a few KB.
_TOP_KEYWORDS: Final[int] = 25

# Same minimum-token-length rule as :func:`app.keywords.top_keywords`.
_MIN_TOKEN_LENGTH: Final[int] = 4


class KeywordDelta(TypedDict):
    """One row in ``top_new_keywords`` / ``top_gone_keywords``."""

    word: str
    count: int
    delta: int


@dataclass(frozen=True, slots=True)
class MultiDayDiff:
    """Result of comparing the activity of two calendar days.

    Attributes:
        day_a: ISO date string of the "before" / "left" day.
        day_b: ISO date string of the "after" / "right" day.
        new_apps: Apps captured on ``day_b`` but not on ``day_a``.
        gone_apps: Apps captured on ``day_a`` but not on ``day_b``.
        new_tags: Tags applied on ``day_b`` but not on ``day_a``.
        gone_tags: Tags applied on ``day_a`` but not on ``day_b``.
        top_new_keywords: OCR/notes tokens whose B-count exceeds A-count
            the most, sorted by ``delta`` descending. Capped at
            :data:`_TOP_KEYWORDS`.
        top_gone_keywords: Same idea, reversed (A heavier than B).
        shots_a: Total screenshots captured on ``day_a`` — handy for the
            template header.
        shots_b: Same for ``day_b``.
    """

    day_a: str
    day_b: str
    new_apps: list[str]
    gone_apps: list[str]
    new_tags: list[str]
    gone_tags: list[str]
    top_new_keywords: list[KeywordDelta]
    top_gone_keywords: list[KeywordDelta]
    shots_a: int
    shots_b: int

    def as_dict(self) -> dict[str, Any]:
        """Plain-dict view for FastAPI's ``JSONResponse``."""
        return {
            "day_a": self.day_a,
            "day_b": self.day_b,
            "new_apps": self.new_apps,
            "gone_apps": self.gone_apps,
            "new_tags": self.new_tags,
            "gone_tags": self.gone_tags,
            "top_new_keywords": [dict(item) for item in self.top_new_keywords],
            "top_gone_keywords": [dict(item) for item in self.top_gone_keywords],
            "shots_a": self.shots_a,
            "shots_b": self.shots_b,
        }


def _parse_day(value: str) -> str:
    """Validate ``value`` as ``YYYY-MM-DD`` and return the canonical form.

    Raises :class:`ValueError` for anything that ``date.fromisoformat``
    can't parse. We then re-emit the parsed value as ISO so e.g.
    ``"2026-6-3"`` is normalised to ``"2026-06-03"``. This keeps the SQL
    ``DATE(captured_at) = ?`` comparison robust even when callers pass
    sloppy input.
    """
    parsed = date.fromisoformat(value)
    return parsed.isoformat()


async def compare_days(day_a: str, day_b: str) -> dict[str, Any]:
    """Compare two calendar days and return a diff payload as a dict.

    Args:
        day_a: ISO date string (``YYYY-MM-DD``) for the "before" side.
        day_b: ISO date string for the "after" side.

    Returns:
        A dict matching :meth:`MultiDayDiff.as_dict` with keys
        ``new_apps``, ``gone_apps``, ``new_tags``, ``gone_tags``,
        ``top_new_keywords``, ``top_gone_keywords``, plus the echoed day
        strings and per-side shot totals.

    Raises:
        ValueError: When either date string fails ISO parsing.
    """
    parsed_a = _parse_day(day_a)
    parsed_b = _parse_day(day_b)

    async with get_connection() as conn:
        apps_a = await _apps_for_day(conn, parsed_a)
        apps_b = await _apps_for_day(conn, parsed_b)
        tags_a = await _tags_for_day(conn, parsed_a)
        tags_b = await _tags_for_day(conn, parsed_b)
        counter_a = await _keyword_counter_for_day(conn, parsed_a)
        counter_b = await _keyword_counter_for_day(conn, parsed_b)
        shots_a = await _shot_count_for_day(conn, parsed_a)
        shots_b = await _shot_count_for_day(conn, parsed_b)

    new_apps = sorted(apps_b - apps_a)
    gone_apps = sorted(apps_a - apps_b)
    new_tags = sorted(tags_b - tags_a)
    gone_tags = sorted(tags_a - tags_b)

    top_new_keywords = _top_delta(counter_b, counter_a)
    top_gone_keywords = _top_delta(counter_a, counter_b)

    result = MultiDayDiff(
        day_a=parsed_a,
        day_b=parsed_b,
        new_apps=new_apps,
        gone_apps=gone_apps,
        new_tags=new_tags,
        gone_tags=gone_tags,
        top_new_keywords=top_new_keywords,
        top_gone_keywords=top_gone_keywords,
        shots_a=shots_a,
        shots_b=shots_b,
    )

    log.info(
        "multi_day_diff.computed",
        day_a=parsed_a,
        day_b=parsed_b,
        shots_a=shots_a,
        shots_b=shots_b,
        new_apps=len(new_apps),
        gone_apps=len(gone_apps),
        new_tags=len(new_tags),
        gone_tags=len(gone_tags),
        top_new_keywords=len(top_new_keywords),
        top_gone_keywords=len(top_gone_keywords),
    )

    return result.as_dict()


async def _apps_for_day(conn: aiosqlite.Connection, day_iso: str) -> set[str]:
    """Set of non-null ``app_name`` values seen on ``day_iso``."""
    cursor = await conn.execute(
        "SELECT DISTINCT app_name FROM screenshots "
        "WHERE DATE(captured_at) = ? AND app_name IS NOT NULL AND app_name != ''",
        (day_iso,),
    )
    rows = await cursor.fetchall()
    return {str(row["app_name"]) for row in rows}


async def _tags_for_day(conn: aiosqlite.Connection, day_iso: str) -> set[str]:
    """Set of tag names applied to screenshots captured on ``day_iso``."""
    cursor = await conn.execute(
        "SELECT DISTINCT t.name FROM tags t "
        "JOIN screenshot_tags st ON st.tag_id = t.id "
        "JOIN screenshots s ON s.id = st.screenshot_id "
        "WHERE DATE(s.captured_at) = ?",
        (day_iso,),
    )
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


async def _shot_count_for_day(conn: aiosqlite.Connection, day_iso: str) -> int:
    """Total screenshots whose ``captured_at`` falls on ``day_iso``."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots WHERE DATE(captured_at) = ?",
        (day_iso,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _keyword_counter_for_day(
    conn: aiosqlite.Connection,
    day_iso: str,
) -> Counter[str]:
    """Token frequency across OCR + notes for ``day_iso``.

    Stopwords + minimum length match :func:`app.keywords.top_keywords`
    so the per-day picture aligns with the lookback-window page.
    """
    counter: Counter[str] = Counter()

    cursor = await conn.execute(
        "SELECT ocr_text FROM screenshots "
        "WHERE DATE(captured_at) = ? "
        "AND ocr_text IS NOT NULL AND ocr_text != ''",
        (day_iso,),
    )
    async for row in cursor:
        _absorb_tokens(counter, str(row["ocr_text"]))

    # Notes are an optional table (migration 002); if it's not present
    # we simply skip — the diff still works on OCR alone.
    try:
        notes_cursor = await conn.execute(
            "SELECT body FROM screenshot_notes "
            "WHERE DATE(created_at) = ? "
            "AND body IS NOT NULL AND body != ''",
            (day_iso,),
        )
    except aiosqlite.OperationalError as exc:
        log.debug("multi_day_diff.notes_missing", error=str(exc))
        return counter

    async for row in notes_cursor:
        _absorb_tokens(counter, str(row["body"]))

    return counter


def _absorb_tokens(counter: Counter[str], text: str) -> None:
    """Tokenise ``text`` and bump ``counter`` for every keeper token."""
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum()).lower()
        if len(cleaned) < _MIN_TOKEN_LENGTH or cleaned in STOPWORDS:
            continue
        counter[cleaned] += 1


def _top_delta(
    heavier: Counter[str],
    lighter: Counter[str],
) -> list[KeywordDelta]:
    """Return the top ``_TOP_KEYWORDS`` tokens where ``heavier`` > ``lighter``.

    The ranking is by the *positive* delta — so a word that appears 50
    times on day B and 5 times on day A ranks above a word that appears
    20 times on B but never on A. This matches the "what shifted" framing
    we surface in the template rather than a strict set-difference, which
    would over-weight one-shot OCR noise.
    """
    deltas: list[KeywordDelta] = []
    for word, heavier_count in heavier.items():
        diff = heavier_count - lighter.get(word, 0)
        if diff <= 0:
            continue
        deltas.append(
            {
                "word": word,
                "count": heavier_count,
                "delta": diff,
            }
        )
    deltas.sort(key=lambda item: (-item["delta"], -item["count"], item["word"]))
    return deltas[:_TOP_KEYWORDS]
