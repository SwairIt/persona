"""OCR email extraction — pull email addresses out of arbitrary OCR text.

A deliberately small, dependency-free helper that mirrors the pattern used by
:mod:`app.keywords` and :mod:`app.phrase_frequency`: one synchronous
:func:`extract_emails` function backed by a single :func:`re.findall` call,
plus an async aggregator (:func:`email_mentions`) over the last *N* days for
the ``/stats/emails`` page.

The regex is intentionally conservative — it matches the dominant ``RFC 5322``
shape that survives Tesseract's whitespace gymnastics without trying to be
exhaustive. False positives from OCR noise (e.g. ``foo@bar`` with no TLD) are
rejected by requiring at least one dot in the domain and a 2+ char TLD.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Final

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.ocr.emails")


# RFC-5322-ish "local@domain.tld" pattern. The character class for the local
# part covers the common subset (alnum + dot/underscore/percent/plus/hyphen);
# the domain part requires a dotted form with a 2+ char alphabetic TLD so
# stray OCR fragments like ``user@host`` don't slip through.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)


def extract_emails(text: str) -> list[str]:
    """Return de-duplicated email addresses found in ``text``.

    Args:
        text: Arbitrary text — typically the ``ocr_text`` column of a
            screenshot. ``None`` / empty / non-``str`` inputs return ``[]``.

    Returns:
        Lowercased email addresses in *first-seen* order. Duplicates are
        dropped while preserving the relative ordering of unique entries,
        so callers can render a stable chip list without re-sorting.
    """
    if not text or not isinstance(text, str):
        return []

    seen: set[str] = set()
    ordered: list[str] = []
    for match in _EMAIL_RE.findall(text):
        normalised = match.lower()
        if normalised in seen:
            continue
        seen.add(normalised)
        ordered.append(normalised)
    return ordered


async def email_mentions(days: int = 30) -> list[dict[str, int | str]]:
    """Aggregate email mentions across OCR text over the last ``days``.

    Each unique address is counted once per screenshot it appears in, even
    if the OCR text mentions it multiple times — this matches the "mentions"
    intuition (how many shots reference ``foo@bar.com``) better than raw
    occurrence count and is robust against rendered email-signature blocks
    that tile the same address dozens of times.

    Args:
        days: Look-back window in days (inclusive of "now").

    Returns:
        ``[{"email": str, "count": int}, ...]`` sorted by count descending,
        ties broken alphabetically. Empty list when no emails are found.
    """
    if days <= 0:
        log.warning("ocr.emails.invalid_window", days=days)
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
            for email in extract_emails(str(row["ocr_text"])):
                counter[email] += 1

    items: list[dict[str, int | str]] = [
        {"email": email, "count": count}
        for email, count in sorted(
            counter.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
    ]

    log.info(
        "ocr.emails.aggregated",
        days=days,
        shots_scanned=shots_scanned,
        unique_emails=len(items),
    )
    return items
