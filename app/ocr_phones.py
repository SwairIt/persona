"""OCR phone-number extraction — pull phone numbers out of arbitrary OCR text.

Sibling to :mod:`app.ocr_emails`: one synchronous :func:`extract_phones`
function backed by a single :func:`re.findall` call, plus an async aggregator
(:func:`phone_mentions`) over the last *N* days for the ``/stats/phones`` page.

The regex is deliberately pragmatic rather than exhaustive. It targets the
shapes that survive Tesseract's whitespace gymnastics on real screenshots —
``+1 (415) 555-2671``, ``+7 (495) 123-45-67``, ``8-800-555-35-35``,
``+44 7700 900123``, ``(212) 555-0100`` etc. — without trying to validate
country-specific numbering plans (a job for ``phonenumbers``, intentionally
out of scope for this dependency-free helper).

False positives from version strings, dates, and isolated number runs are
rejected by:

* requiring 7-15 digits in total (E.164 limits),
* requiring either a leading ``+`` / ``8`` / ``00`` *or* a parenthesised
  area-code prefix, so a flat ``2024 12 31`` doesn't masquerade as a number,
* normalising each match to ``+digits`` (or just ``digits`` when no country
  code was given) so OCR variants of the same number collapse for dedup.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Final

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.ocr.phones")


# Two alternatives are OR'd into one pattern:
#
# 1. International / leading-zero forms:
#    ``+1 (415) 555-2671``, ``+7 495 123-45-67``, ``8-800-555-35-35``,
#    ``00 44 7700 900123``. Anchor the first token to ``+``, ``8`` or ``00``
#    so we don't grab arbitrary 7-digit runs from version strings.
#
# 2. Parenthesised area-code without country code:
#    ``(212) 555-0100``. The parens act as the discriminator that this is
#    a phone, not a measurement.
#
# Both branches allow spaces, hyphens and dots between digit groups. Total
# digit count is checked in Python (regex character-class arithmetic for
# "7-15 digits ignoring separators" is unreadable).
_PHONE_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    (?<![\w])                                # left boundary, no word char
    (?:
        (?:\+|00|8)                          # country-code lead
        [\s\-.]?
        (?:\(?\d{1,4}\)?[\s\-.]?){1,2}       # 1-2 area/operator groups
        \d{2,4}[\s\-.]?\d{2,4}               # subscriber part 1
        (?:[\s\-.]?\d{2,4})?                 # optional subscriber part 2
    |
        \(\d{2,5}\)[\s\-.]?                  # required (area-code) discriminator
        \d{2,4}[\s\-.]?\d{2,4}
        (?:[\s\-.]?\d{2,4})?
    )
    (?![\w])                                 # right boundary, no word char
    """,
    re.VERBOSE,
)

_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"\d")

# E.164 caps at 15 digits; 7 is the lowest sane subscriber length we'll accept
# (shorter "numbers" in OCR are almost always serials or codes).
_MIN_DIGITS: Final[int] = 7
_MAX_DIGITS: Final[int] = 15


def _normalise(raw: str) -> str | None:
    """Collapse a raw phone match to ``+digits`` (or ``digits``) for dedup.

    Returns ``None`` when the digit count falls outside E.164's 7-15 window —
    the caller drops these. A leading ``+`` is preserved; OCR shorthand of
    ``8`` for Russia and ``00`` for international dialling are normalised to
    ``+7`` / ``+`` so the same number written three ways collapses to one
    canonical chip on the UI.
    """
    digits = "".join(_DIGITS_RE.findall(raw))
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return None

    stripped = raw.lstrip()
    if stripped.startswith("+"):
        return "+" + digits
    if stripped.startswith("00"):
        return "+" + digits[2:]
    if stripped.startswith("8") and len(digits) == 11:
        # RU national → E.164: 8XXXXXXXXXX -> +7XXXXXXXXXX
        return "+7" + digits[1:]
    return digits


def extract_phones(text: str) -> list[str]:
    """Return de-duplicated phone numbers found in ``text``.

    Args:
        text: Arbitrary text — typically the ``ocr_text`` column of a
            screenshot. ``None`` / empty / non-``str`` inputs return ``[]``.

    Returns:
        Canonicalised phone numbers in *first-seen* order. Each entry is
        either ``"+<digits>"`` (international form) or ``"<digits>"``
        (when no country code was detectable). Duplicates are dropped
        while preserving ordering so callers can render a stable chip list
        without re-sorting.
    """
    if not text or not isinstance(text, str):
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for match in _PHONE_RE.findall(text):
        normalised = _normalise(match)
        if normalised is None or normalised in seen:
            continue
        seen.add(normalised)
        ordered.append(normalised)
    return ordered


async def phone_mentions(days: int = 30) -> list[dict[str, int | str]]:
    """Aggregate phone-number mentions across OCR text over the last ``days``.

    Each unique number is counted once per screenshot it appears in, even
    if the OCR text mentions it multiple times — same "mentions" intuition
    as :func:`app.ocr_emails.email_mentions`. Numbers are canonicalised via
    :func:`_normalise` so OCR variants of the same number (``+7 495 ...``
    vs ``8-495-...``) collapse to a single counter entry.

    Args:
        days: Look-back window in days (inclusive of "now").

    Returns:
        ``[{"phone": str, "count": int}, ...]`` sorted by count descending,
        ties broken alphabetically. Empty list when no phones are found.
    """
    if days <= 0:
        log.warning("ocr.phones.invalid_window", days=days)
        return []

    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_iso = iso(cutoff)

    counter: Counter[str] = Counter()
    shots_scanned = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE captured_at >= ? AND ocr_text IS NOT NULL AND ocr_text != ''",
            (cutoff_iso,),
        )
        async for row in cursor:
            shots_scanned += 1
            for phone in extract_phones(str(row["ocr_text"])):
                counter[phone] += 1

    items: list[dict[str, int | str]] = [
        {"phone": phone, "count": count}
        for phone, count in sorted(
            counter.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
    ]

    log.info(
        "ocr.phones.aggregated",
        days=days,
        shots_scanned=shots_scanned,
        unique_phones=len(items),
    )
    return items
