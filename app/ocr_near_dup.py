"""OCR near-duplicate detection — Jaccard similarity on word tokens.

Persona v0.44 feature 2/3. Finds clusters of screenshots whose OCR text
is "almost the same" — same chat thread re-screenshotted seconds apart,
the same slide auto-captured twice, the same error dialog redrawn on
focus changes, etc. Lets the admin review pairs and soft-delete the
redundant one (via :mod:`app.recycle` — the v0.40 recycle bin), so the
user never loses the row if the judgement was wrong.

Algorithm
---------
1. Pull every screenshot from the last ``days`` window whose ``ocr_text``
   is non-empty. Cap the candidate set at ``_MAX_SHOTS`` to keep the
   ``O(N^2)`` pairwise loop tractable — past that we bail out and let
   the caller narrow the window.
2. Tokenise each ``ocr_text`` once: lowercase, split on non-alphanumeric
   characters (Unicode-aware via ``\\W``), drop tokens shorter than three
   characters. Stored as a Python :class:`set` per shot so the inner
   loop is set-arithmetic only.
3. Walk every unordered pair. Skip pairs where either side has fewer
   than ``_MIN_TOKENS`` tokens (an OCR fragment of two words is noise,
   not signal). Compute Jaccard = ``|A & B| / |A | B|``.
4. Keep pairs where ``jaccard >= min_jaccard``, sort descending by
   score, truncate to ``max_pairs`` and return.

Pure CPU work — the function is ``async`` only to share the project's
``get_connection`` async context manager. Tokenisation and pair counting
happen on the event loop because the cap (``_MAX_SHOTS = 2000``) keeps
the worst-case at ~2M iterations of set intersection on small sets —
well below a second even on a slow VM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr.near_dup")

# Hard cap on the candidate set. The pairwise loop is O(N^2); at 2000
# shots that's ~2M comparisons of small sets — still under a second on
# commodity hardware. Past this we refuse to compute and ask the caller
# to narrow ``days``, so the admin page never times out silently.
_MAX_SHOTS: Final[int] = 2000

# Tokens below this length are mostly OCR noise (single letters, "of",
# "in", line-number digits) — they inflate intersections without adding
# real signal. Threshold matches the v0.34 ocr_diff word splitter.
_MIN_TOKEN_LEN: Final[int] = 3

# Skip pairs where either side has fewer than this many tokens. A shot
# with a four-word OCR result can trivially Jaccard-match anything that
# happens to share those four words — not a real near-duplicate.
_MIN_TOKENS: Final[int] = 5

# Hard ceilings on the public knobs so a hand-typed querystring can't
# request "all of history" or a negative threshold that bypasses the
# filter entirely.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 3650
_MIN_PAIRS: Final[int] = 1
_MAX_PAIRS: Final[int] = 1000

# Splits on every run of non-word characters. ``\W`` is Unicode-aware
# under Python 3, so Cyrillic / accented Latin / CJK tokens survive the
# split intact — we only drop punctuation and whitespace.
_TOKEN_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\W+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class NearDuplicatePair:
    """One near-duplicate pair returned by :func:`find_near_duplicates`.

    The dataclass is frozen so callers cannot mutate the result list in
    place — important because the page template iterates the same list
    multiple times (once for the table, once for the count).
    """

    a_id: int
    b_id: int
    jaccard: float
    a_app: str | None
    b_app: str | None
    a_captured_at: str | None
    b_captured_at: str | None
    a_thumbnail: str | None
    b_thumbnail: str | None

    def as_dict(self) -> dict[str, object]:
        """Plain-dict view for templates / JSON serialisation."""
        return {
            "a_id": self.a_id,
            "b_id": self.b_id,
            "jaccard": self.jaccard,
            "a_app": self.a_app,
            "b_app": self.b_app,
            "a_captured_at": self.a_captured_at,
            "b_captured_at": self.b_captured_at,
            "a_thumbnail": self.a_thumbnail,
            "b_thumbnail": self.b_thumbnail,
        }


def _tokenise(text: str) -> set[str]:
    """Lowercase, split on non-alphanumeric runs, drop short tokens.

    Returns a :class:`set` — the Jaccard loop only ever needs membership
    and ``|`` / ``&``, never order or frequency. Empty input yields an
    empty set (so the caller's "skip if too few tokens" check still
    works without a None guard).
    """
    if not text:
        return set()
    lowered = text.lower()
    tokens = _TOKEN_SPLIT_RE.split(lowered)
    return {tok for tok in tokens if len(tok) >= _MIN_TOKEN_LEN}


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp ``value`` into ``[low, high]``."""
    if value < low:
        return low
    if value > high:
        return high
    return value


def _clamp_jaccard(value: float) -> float:
    """Clamp the similarity threshold into ``(0.0, 1.0]``.

    A threshold of ``0.0`` would return every pair sharing a single
    token — useless noise. A threshold above ``1.0`` is impossible by
    definition. We pin to ``0.05 .. 1.0`` so the slider in the UI has a
    sane floor.
    """
    if value < 0.05:
        return 0.05
    if value > 1.0:
        return 1.0
    return float(value)


async def find_near_duplicates(
    days: int = 7,
    min_jaccard: float = 0.85,
    max_pairs: int = 200,
) -> list[NearDuplicatePair]:
    """Find OCR near-duplicate pairs in the last ``days`` days.

    Args:
        days: Trailing window in days. Clamped to ``[1, 3650]``.
        min_jaccard: Similarity floor. Pairs scoring strictly below this
            are dropped. Clamped to ``[0.05, 1.0]``.
        max_pairs: Max length of the returned list. Clamped to
            ``[1, 1000]``. Results are sorted by descending Jaccard so
            truncation always keeps the strongest matches.

    Returns:
        A list of :class:`NearDuplicatePair`, descending by Jaccard.
        Empty list when no shots qualify or the candidate set was
        empty. When more than ``_MAX_SHOTS`` shots match the window
        the function logs a warning and returns ``[]`` so the admin
        page can render a "narrow the window" notice.
    """
    window_days = _clamp(days, _MIN_DAYS, _MAX_DAYS)
    threshold = _clamp_jaccard(min_jaccard)
    cap = _clamp(max_pairs, _MIN_PAIRS, _MAX_PAIRS)

    modifier = f"-{window_days} days"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, ocr_text, app_name, captured_at, thumbnail_path "
            "FROM screenshots "
            "WHERE ocr_text IS NOT NULL "
            "AND TRIM(ocr_text) != '' "
            "AND captured_at >= datetime('now', ?) "
            "ORDER BY captured_at DESC",
            (modifier,),
        )
        # ``fetchall()`` is typed ``Iterable[Row]`` — materialise once so
        # the length check and the loop below both see the same list and
        # ``len()`` is well-defined under mypy strict.
        rows = list(await cursor.fetchall())

    if not rows:
        log.info("ocr.near_dup.empty_window", days=window_days)
        return []

    if len(rows) > _MAX_SHOTS:
        log.warning(
            "ocr.near_dup.too_many_shots",
            shots=len(rows),
            cap=_MAX_SHOTS,
            days=window_days,
        )
        return []

    # Pre-tokenise once. The pairwise loop below reads each set many
    # times — we never want to re-split a 4 KB OCR blob on every pair.
    shots: list[tuple[int, set[str], str | None, str | None, str | None]] = []
    for row in rows:
        tokens = _tokenise(str(row["ocr_text"]))
        if len(tokens) < _MIN_TOKENS:
            continue
        shots.append(
            (
                int(row["id"]),
                tokens,
                str(row["app_name"]) if row["app_name"] is not None else None,
                str(row["captured_at"]) if row["captured_at"] is not None else None,
                str(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None,
            )
        )

    pairs: list[NearDuplicatePair] = []
    n = len(shots)
    for i in range(n):
        a_id, a_tokens, a_app, a_at, a_thumb = shots[i]
        for j in range(i + 1, n):
            b_id, b_tokens, b_app, b_at, b_thumb = shots[j]
            inter = len(a_tokens & b_tokens)
            if inter == 0:
                continue
            union = len(a_tokens | b_tokens)
            # ``union`` is non-zero whenever ``inter`` is non-zero, but
            # mypy can't prove it — explicit guard keeps the division
            # safe under strict mode.
            if union == 0:  # pragma: no cover — defensive guard
                continue
            jaccard = inter / union
            if jaccard < threshold:
                continue
            pairs.append(
                NearDuplicatePair(
                    a_id=a_id,
                    b_id=b_id,
                    jaccard=jaccard,
                    a_app=a_app,
                    b_app=b_app,
                    a_captured_at=a_at,
                    b_captured_at=b_at,
                    a_thumbnail=a_thumb,
                    b_thumbnail=b_thumb,
                )
            )

    pairs.sort(key=lambda p: p.jaccard, reverse=True)
    truncated = pairs[:cap]

    log.info(
        "ocr.near_dup.computed",
        days=window_days,
        threshold=threshold,
        candidates=n,
        pairs_found=len(pairs),
        pairs_returned=len(truncated),
    )
    return truncated


__all__ = ["NearDuplicatePair", "find_near_duplicates"]
