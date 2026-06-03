"""Naive lexicon-based sentiment scoring for OCR text.

v1.6 feature 3/3. Produces a single floating-point polarity score in
``[-1.0, +1.0]`` for an arbitrary piece of OCR-extracted text. The
score is *intentionally* shallow:

* Tokenisation is whitespace + ASCII punctuation — no stemming, no
  PoS tagging, no language detection. OCR text on a typical developer
  desktop is overwhelmingly English short strings (commit subjects,
  CI logs, Slack threads), and the bundled lexicon was hand-picked
  against exactly that distribution.
* Polarity is computed as ``sum(polarities) / max(1, hit_count)`` —
  the mean polarity over matched tokens. Dividing by ``max(1, n)``
  protects against zero-divide on text with no lexicon hits and also
  keeps the per-token contribution stable across short and long
  inputs (a single negative word in a one-line shot scores the same
  as the same word surrounded by neutral filler in a long shot).
* Negation, intensifiers, sarcasm, and multi-word phrases are *not*
  handled. Doing so would require a real NLP toolkit, which we
  explicitly avoid to keep the install footprint zero-dependency.

The output is therefore best understood as a *signal*, not a verdict:
it surfaces broad mood swings in a 30-day window without claiming to
understand any individual screen. Empty text and text with no lexicon
hits score ``0.0`` — the caller (typically
:mod:`app.workers.ocr_worker`) decides whether to persist that zero
or leave the column ``NULL`` to mean "no signal".

Lexicon size is roughly 80 positive + 80 negative entries; every
word is lowercase-ASCII. The :func:`score` function lowercases its
input once and then does pure-Python ``set`` lookups, so the cost is
O(tokens) with a small constant factor — fine to call on every OCR
result without batching.
"""

from __future__ import annotations

import re
from typing import Final

from app.logging_setup import get_logger

log = get_logger("persona.ocr.sentiment")

# Tokeniser — collapse anything that isn't an ASCII letter into a
# whitespace boundary, then split. We deliberately *don't* keep Unicode
# letters: the lexicon below is ASCII-only, so a non-ASCII word can
# never match anyway, and ignoring them here keeps the regex tiny and
# the behaviour easy to reason about.
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z']+")

# Clamp bounds — the algebra of ``mean(polarities)`` already keeps the
# output in ``[-1, +1]``, but float arithmetic on long inputs can drift
# by a ULP or two. Clamp at the boundary so persisted values are always
# strictly inside the documented range.
_MIN_SCORE: Final[float] = -1.0
_MAX_SCORE: Final[float] = 1.0

# Hand-curated positive lexicon (~80 entries) — words the OCR worker
# is most likely to see on a developer/operator desktop when something
# is going right. Kept as a frozenset for O(1) membership checks; the
# literal is sorted alphabetically per-block so adding entries during
# review is a clean diff.
_POSITIVE: Final[frozenset[str]] = frozenset(
    {
        # Build / ship / done outcomes
        "achieved",
        "approved",
        "complete",
        "completed",
        "done",
        "fixed",
        "landed",
        "launch",
        "launched",
        "merged",
        "passed",
        "passing",
        "released",
        "ship",
        "shipped",
        "shipping",
        "solved",
        "succeeded",
        "success",
        "successful",
        "successfully",
        "win",
        "winning",
        "won",
        # General affect / praise
        "amazing",
        "awesome",
        "beautiful",
        "best",
        "brilliant",
        "celebrate",
        "celebrated",
        "cheer",
        "delight",
        "delighted",
        "elegant",
        "enjoy",
        "enjoyed",
        "excellent",
        "fantastic",
        "favorite",
        "favourite",
        "glad",
        "good",
        "great",
        "happy",
        "joy",
        "kudos",
        "love",
        "loved",
        "lovely",
        "nice",
        "outstanding",
        "perfect",
        "pleasant",
        "pleased",
        "positive",
        "praise",
        "proud",
        "smooth",
        "solid",
        "splendid",
        "stable",
        "stellar",
        "stoked",
        "superb",
        "thanks",
        "thankful",
        "thrilled",
        "wonderful",
        "yay",
        # Productivity wins
        "boost",
        "boosted",
        "clean",
        "clear",
        "efficient",
        "fast",
        "faster",
        "fresh",
        "improved",
        "improvement",
        "optimal",
        "progress",
        "ready",
        "resolved",
    }
)

# Hand-curated negative lexicon (~80 entries) — words associated with
# broken builds, failed jobs, urgent incidents, and frustration. Same
# structure as ``_POSITIVE``: alphabetised by block, frozenset for
# constant-time lookup.
_NEGATIVE: Final[frozenset[str]] = frozenset(
    {
        # Build / ship / done failures
        "broken",
        "bug",
        "bugs",
        "buggy",
        "crash",
        "crashed",
        "crashing",
        "denied",
        "down",
        "error",
        "errors",
        "fail",
        "failed",
        "failing",
        "failure",
        "fatal",
        "fault",
        "flaky",
        "frozen",
        "halt",
        "halted",
        "hang",
        "hanging",
        "leak",
        "lose",
        "losing",
        "lost",
        "outage",
        "regression",
        "reverted",
        "rollback",
        "stuck",
        "timeout",
        "unstable",
        # General affect / frustration
        "afraid",
        "angry",
        "annoyed",
        "annoying",
        "awful",
        "bad",
        "blocker",
        "blocked",
        "concern",
        "concerned",
        "confused",
        "disappointed",
        "doubt",
        "dread",
        "fear",
        "frustrated",
        "frustrating",
        "garbage",
        "hate",
        "hated",
        "horrible",
        "miserable",
        "negative",
        "painful",
        "panic",
        "poor",
        "sad",
        "scary",
        "slow",
        "stressed",
        "stupid",
        "suck",
        "sucks",
        "terrible",
        "tired",
        "ugly",
        "unhappy",
        "upset",
        "useless",
        "worried",
        "worst",
        # Severity markers
        "alert",
        "critical",
        "danger",
        "emergency",
        "exception",
        "invalid",
        "missing",
        "severe",
        "urgent",
        "warning",
    }
)


def _clamp(value: float) -> float:
    """Clamp ``value`` into ``[_MIN_SCORE, _MAX_SCORE]``.

    Defensive guard against floating-point drift on long inputs — the
    mathematical bound on ``mean(±1)`` is exactly ``[-1, +1]`` but a
    few thousand additions and a final division can leave the result
    a ULP or two outside.
    """
    if value < _MIN_SCORE:
        return _MIN_SCORE
    if value > _MAX_SCORE:
        return _MAX_SCORE
    return value


def score(text: str | None) -> float:
    """Compute a naive polarity score in ``[-1.0, +1.0]`` for ``text``.

    Empty / whitespace-only input scores ``0.0`` immediately. Otherwise
    the text is lowercased once and tokenised by :data:`_TOKEN_PATTERN`;
    every token is checked against :data:`_POSITIVE` (``+1.0``) and
    :data:`_NEGATIVE` (``-1.0``); the running sum is divided by
    ``max(1, hit_count)`` so a no-hit input scores ``0.0`` rather than
    blowing up on a zero divisor.

    The same token contributes once *per occurrence* — repeating
    ``"bug bug bug"`` produces ``-1.0`` exactly, which matches the
    intuition that a screenful of one negative word is as negative as
    that signal can possibly get.

    Args:
        text: The OCR-extracted text to score. ``None`` is treated as
              the empty string.

    Returns:
        A float in ``[-1.0, +1.0]`` rounded to four decimal places.
        The rounding keeps the persisted value stable across re-scores
        of the same input even if the lexicon picks up a no-op
        reordering later.
    """
    if not text:
        return 0.0

    lowered = text.lower()
    polarity_sum = 0
    hit_count = 0
    for token in _TOKEN_PATTERN.findall(lowered):
        if token in _POSITIVE:
            polarity_sum += 1
            hit_count += 1
        elif token in _NEGATIVE:
            polarity_sum -= 1
            hit_count += 1

    if hit_count == 0:
        return 0.0

    raw = polarity_sum / max(1, hit_count)
    clamped = _clamp(raw)
    # Round to four decimals: ``mean`` over up to a few hundred tokens
    # never needs more precision and the trimmed form is friendlier in
    # logs / JSON payloads.
    return round(clamped, 4)


__all__ = [
    "score",
]
