"""OCR bigram / trigram phrase frequency — most-common n-grams across recent OCR text.

A sibling of :mod:`app.keywords` that counts *adjacent* token sequences instead of
individual words. Useful for surfacing recurring multi-word phrases ("error
message", "pull request", "родительский контроль") that single-token frequency
analysis flattens into noise.

The :data:`STOPWORDS` set is re-used from v0.28 (:mod:`app.keywords`) so the
two endpoints stay in sync — extend the master list there and bigrams pick it
up automatically.

Results are returned as ``[{"phrase": str, "count": int}, ...]`` so the JSON
endpoint and Jinja template can consume them without bespoke serialisation.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from app.keywords import STOPWORDS, _tokenise
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.phrase_frequency")


# Defensive caps so a hand-crafted URL can't scan the whole archive or build a
# Counter the size of the working set. Matches the keywords module conventions.
_MAX_DAYS: int = 365
_MAX_TOP_N: int = 500
_MIN_NGRAM: int = 2
_MAX_NGRAM: int = 3


def _is_meaningful(token: str, min_length: int) -> bool:
    """Return ``True`` if ``token`` is long enough and not a stopword."""
    return len(token) >= min_length and token not in STOPWORDS


def _ngrams(tokens: list[str], n_gram: int, min_length: int) -> list[str]:
    """Yield n-gram phrases from ``tokens``.

    Only n-grams whose *first* and *last* tokens are meaningful (>= ``min_length``
    and not in :data:`STOPWORDS`) are emitted. Interior stopwords are tolerated so
    phrases like ``"sign in to"`` survive — they're the whole point of bigram /
    trigram analysis.
    """
    if n_gram < _MIN_NGRAM or len(tokens) < n_gram:
        return []
    phrases: list[str] = []
    for i in range(len(tokens) - n_gram + 1):
        window = tokens[i : i + n_gram]
        if not _is_meaningful(window[0], min_length):
            continue
        if not _is_meaningful(window[-1], min_length):
            continue
        phrases.append(" ".join(window))
    return phrases


async def top_phrases(
    days: int = 7,
    n_gram: int = 2,
    top_n: int = 30,
    min_length: int = 3,
) -> list[dict[str, int | str]]:
    """Return the ``top_n`` most frequent n-gram phrases from OCR text.

    Args:
        days: Look-back window in days (inclusive of "now"). Clamped to
            ``[1, 365]``.
        n_gram: Phrase length. Currently supports ``2`` (bigrams) or ``3``
            (trigrams).
        top_n: Maximum number of (phrase, count) pairs to return. Clamped to
            ``[1, 500]``.
        min_length: Drop *anchor* tokens (first / last in the window) shorter
            than this after cleaning. Interior tokens are not filtered.

    Returns:
        ``[{"phrase": str, "count": int}, ...]`` sorted by count descending.
        An empty list is returned when no text is found or parameters are
        out of range.
    """
    if days <= 0 or top_n <= 0 or min_length <= 0:
        log.warning(
            "phrase_frequency.invalid_params",
            days=days,
            n_gram=n_gram,
            top_n=top_n,
            min_length=min_length,
        )
        return []
    if n_gram < _MIN_NGRAM or n_gram > _MAX_NGRAM:
        log.warning(
            "phrase_frequency.unsupported_ngram",
            n_gram=n_gram,
            supported=(_MIN_NGRAM, _MAX_NGRAM),
        )
        return []

    days = min(days, _MAX_DAYS)
    top_n = min(top_n, _MAX_TOP_N)

    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_iso = iso(cutoff)

    counter: Counter[str] = Counter()
    ocr_chars = 0
    rows_scanned = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE captured_at >= ? "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (cutoff_iso,),
        )
        async for row in cursor:
            rows_scanned += 1
            text = str(row["ocr_text"])
            ocr_chars += len(text)
            tokens = _tokenise(text)
            for phrase in _ngrams(tokens, n_gram, min_length):
                counter[phrase] += 1

    result: list[dict[str, int | str]] = [
        {"phrase": phrase, "count": count}
        for phrase, count in counter.most_common(top_n)
    ]

    log.info(
        "phrase_frequency.computed",
        days=days,
        n_gram=n_gram,
        top_n=top_n,
        min_length=min_length,
        rows_scanned=rows_scanned,
        ocr_chars=ocr_chars,
        unique_phrases=len(counter),
        returned=len(result),
    )
    return result
