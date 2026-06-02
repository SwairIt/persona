"""OCR language statistics — character-class breakdown of the OCR corpus.

Persona's multi-language OCR (v0.29) accepts a ``+``-joined list of
Tesseract language packs (``eng+rus+...``). This module measures the
*actual* output distribution: how much of the recognised text is
Cyrillic vs Latin vs CJK vs digits vs other glyphs.

The classifier is intentionally a pure-Python codepoint check — no
regex, no third-party language detector — so it stays predictable and
dependency-free. Each character is bucketed into exactly one of:

* ``cyrillic`` — U+0400..U+04FF (Cyrillic) plus U+0500..U+052F
  (Cyrillic Supplement), so ``ё`` and Slavic-region extensions count.
* ``latin``    — basic Latin letters A..Z / a..z plus Latin-1 / Latin
  Extended-A (U+00C0..U+017F) so accented European glyphs land in the
  Latin bucket rather than ``other``.
* ``cjk``      — CJK Unified Ideographs (U+4E00..U+9FFF) and the most
  common Han extension blocks (U+3400..U+4DBF, U+20000..U+2A6DF).
* ``digit``    — any character for which :py:meth:`str.isdigit` is true
  (covers ASCII ``0-9`` and Unicode digit forms).
* ``other``    — everything else, including whitespace, punctuation,
  symbols, and scripts we don't track explicitly.

The aggregation walks ``screenshots.ocr_text`` rows captured within the
last ``days`` days and additionally tracks per-``app_name`` totals so
the UI can show *which apps drive each script*.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.ocr.lang_stats")


# ---------------------------------------------------------------------------
# Character classification — pure Python codepoint ranges.
# ---------------------------------------------------------------------------

# Cyrillic main block + Cyrillic Supplement. Covers Russian, Ukrainian,
# Bulgarian, Serbian, plus less-common Slavic extensions.
_CYRILLIC_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0400, 0x04FF),
    (0x0500, 0x052F),
)

# Basic Latin letters A-Z / a-z and the most common accented European
# letters from Latin-1 Supplement and Latin Extended-A.
_LATIN_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x0041, 0x005A),  # A..Z
    (0x0061, 0x007A),  # a..z
    (0x00C0, 0x024F),  # Latin-1 + Extended-A + Extended-B letters
)

# CJK Unified Ideographs main block + Extension A + Extension B start.
# Hiragana/katakana intentionally excluded — they're a different script.
_CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3400, 0x4DBF),    # CJK Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0x20000, 0x2A6DF),  # CJK Extension B
)

# Top-app list size per script (mirrors the UI's "top 5" presentation).
_TOP_APPS_PER_SCRIPT: Final[int] = 5

# Hard upper bound on the look-back window so an attacker-controlled
# query string can't scan years of OCR text in one request.
_MAX_DAYS: Final[int] = 365


def _in_ranges(codepoint: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    """Return ``True`` iff ``codepoint`` falls inside any ``(lo, hi)`` pair."""
    return any(lo <= codepoint <= hi for lo, hi in ranges)


def _classify(char: str) -> str:
    """Bucket a single character into one of the five script categories.

    The order matters: digits are checked first so a Cyrillic digit
    form (which doesn't exist, but the principle generalises) wouldn't
    silently land in two buckets.
    """
    if char.isdigit():
        return "digit"
    codepoint = ord(char)
    if _in_ranges(codepoint, _CYRILLIC_RANGES):
        return "cyrillic"
    if _in_ranges(codepoint, _LATIN_RANGES):
        return "latin"
    if _in_ranges(codepoint, _CJK_RANGES):
        return "cjk"
    return "other"


# Buckets we surface in the response. Keeping this as an explicit tuple
# guarantees the JSON shape stays stable even when nothing of a given
# script is present in the corpus.
_BUCKETS: Final[tuple[str, ...]] = ("cyrillic", "latin", "cjk", "digit", "other")


class _AppEntry(TypedDict):
    """One row of the per-script top-apps table."""

    app: str
    chars: int


class LanguageBreakdown(TypedDict):
    """Return shape of :func:`language_breakdown`.

    Char counts are absolute integers. ``total_chars`` equals the sum of
    the five bucket counts. ``top_apps_by_language`` is keyed by script
    name and contains the apps that produced the most characters in
    that script.
    """

    cyrillic_chars: int
    latin_chars: int
    cjk_chars: int
    digit_chars: int
    other_chars: int
    total_chars: int
    top_apps_by_language: dict[str, list[_AppEntry]]


async def language_breakdown(days: int = 30) -> LanguageBreakdown:
    """Aggregate OCR character classes over the last ``days`` days.

    Walks every screenshot row with non-empty ``ocr_text`` captured
    within the window, classifies each character, and returns the
    counts as a :class:`LanguageBreakdown` dict.

    Args:
        days: Look-back window in days. Clamped to ``[1, 365]``.

    Returns:
        A :class:`LanguageBreakdown` with zeroed counts when nothing
        was captured in the window. ``top_apps_by_language`` always
        has one key per script, even if the list is empty.
    """
    if days < 1:
        days = 1
    elif days > _MAX_DAYS:
        days = _MAX_DAYS

    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_iso = iso(cutoff)

    totals: Counter[str] = Counter({bucket: 0 for bucket in _BUCKETS})
    per_app: dict[str, Counter[str]] = defaultdict(
        lambda: Counter({bucket: 0 for bucket in _BUCKETS})
    )

    rows_scanned = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, ocr_text FROM screenshots "
            "WHERE captured_at >= ? "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (cutoff_iso,),
        )
        async for row in cursor:
            rows_scanned += 1
            text = str(row["ocr_text"])
            app_name = str(row["app_name"]) if row["app_name"] is not None else "(unknown)"
            local: Counter[str] = Counter()
            for ch in text:
                local[_classify(ch)] += 1
            totals.update(local)
            if local:
                per_app[app_name].update(local)

    top_apps_by_language: dict[str, list[_AppEntry]] = {}
    for bucket in _BUCKETS:
        ranked = sorted(
            (
                (app, counts[bucket])
                for app, counts in per_app.items()
                if counts[bucket] > 0
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )[:_TOP_APPS_PER_SCRIPT]
        top_apps_by_language[bucket] = [
            {"app": app, "chars": chars} for app, chars in ranked
        ]

    total_chars = sum(totals[bucket] for bucket in _BUCKETS)

    result: LanguageBreakdown = {
        "cyrillic_chars": totals["cyrillic"],
        "latin_chars": totals["latin"],
        "cjk_chars": totals["cjk"],
        "digit_chars": totals["digit"],
        "other_chars": totals["other"],
        "total_chars": total_chars,
        "top_apps_by_language": top_apps_by_language,
    }

    log.info(
        "ocr.lang_stats.computed",
        days=days,
        rows_scanned=rows_scanned,
        total_chars=total_chars,
        cyrillic=totals["cyrillic"],
        latin=totals["latin"],
        cjk=totals["cjk"],
        digit=totals["digit"],
        other=totals["other"],
        unique_apps=len(per_app),
    )
    return result
