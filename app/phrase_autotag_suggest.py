"""OCR phrase auto-tag suggester — surface high-frequency phrases as new rules.

Pairs the bigram/trigram counter from :mod:`app.phrase_frequency` (v0.86) with
the literal phrase-tag rules from :mod:`app.ocr_phrase_tags` and proposes the
most popular phrases that aren't already covered. The admin UI renders the
suggestions as one-click "Adopt" buttons that POST to the existing
``/settings/phrase-tags`` form — no schema changes required.

A "suggested tag" is derived from the phrase itself: we lower-case it, drop
non-word characters, and join the survivors with a hyphen so ``"daily
standup"`` becomes ``daily-standup``. Users can edit the tag before adopting;
the suggester just gives a sane default that round-trips through the same
validation as the manual form.

Returns ``[{"phrase": str, "count": int, "suggested_tag": str}, ...]`` so the
template and any future JSON endpoint can consume the data verbatim.
"""

from __future__ import annotations

import re
from typing import TypedDict

from app.logging_setup import get_logger
from app.ocr_phrase_tags import list_rules
from app.phrase_frequency import top_phrases

log = get_logger("persona.phrase_autotag_suggest")


# Defensive caps — mirror the sibling :mod:`app.phrase_frequency` ranges so a
# hand-crafted URL can't ask for "365 days * 500 phrases" and stall the page.
_MAX_DAYS: int = 365
_MAX_TOP_N: int = 200
_DEFAULT_NGRAM: int = 2

# Tag slug: collapse runs of non-word chars to a single hyphen, strip the ends.
# Keeps unicode letters (so Cyrillic phrases survive) by anchoring on ``\w``
# which under Python's re is unicode-aware by default.
_TAG_SLUG_RE: re.Pattern[str] = re.compile(r"\W+", re.UNICODE)


class Suggestion(TypedDict):
    """One row in the suggestions table."""

    phrase: str
    count: int
    suggested_tag: str


def _slugify_tag(phrase: str) -> str:
    """Derive a lowercase hyphenated tag from a free-form phrase.

    The result mirrors what :func:`app.ocr_phrase_tags.add` would accept after
    its own ``strip().lower()`` pass: no leading/trailing whitespace, no
    embedded spaces, lower case.
    """
    lowered = phrase.strip().lower()
    slug = _TAG_SLUG_RE.sub("-", lowered).strip("-")
    return slug


def _existing_phrases(rules: list[dict[str, object]]) -> set[str]:
    """Return the set of phrases already covered by stored rules, lowercased.

    Case-sensitive rules are *still* lowercased for the dedupe check — if the
    admin already wrote a rule for ``"Daily Standup"`` we shouldn't also
    suggest ``"daily standup"`` from OCR noise.
    """
    covered: set[str] = set()
    for rule in rules:
        phrase_value = rule.get("phrase", "")
        phrase_text = str(phrase_value).strip().lower()
        if phrase_text:
            covered.add(phrase_text)
    return covered


async def suggest_rules(
    days: int = 30,
    top_n: int = 20,
    n_gram: int = _DEFAULT_NGRAM,
) -> list[Suggestion]:
    """Return phrase-tag rule suggestions sorted by descending frequency.

    Args:
        days: Look-back window in days for the underlying OCR scan. Clamped to
            ``[1, 365]``.
        top_n: Maximum number of suggestions returned. Clamped to ``[1, 200]``.
        n_gram: Phrase length forwarded to :func:`app.phrase_frequency.top_phrases`.
            Defaults to bigrams.

    Returns:
        ``[{"phrase": str, "count": int, "suggested_tag": str}, ...]`` —
        phrases already covered by existing ``ocr_phrase_tag`` rules are
        filtered out, as are phrases whose derived slug would be empty.
    """
    if days <= 0 or top_n <= 0:
        log.warning(
            "phrase_autotag_suggest.invalid_params",
            days=days,
            top_n=top_n,
            n_gram=n_gram,
        )
        return []

    days = min(days, _MAX_DAYS)
    top_n = min(top_n, _MAX_TOP_N)

    # Over-fetch raw phrases so dedupe against existing rules still leaves us
    # close to ``top_n`` survivors. 3x is a heuristic — large enough that a
    # busy archive with many existing rules still produces a useful list,
    # small enough that we don't scan the entire counter for no reason.
    raw_top_n = min(top_n * 3, _MAX_TOP_N)

    raw_phrases = await top_phrases(
        days=days,
        n_gram=n_gram,
        top_n=raw_top_n,
    )
    existing_rules = await list_rules()
    covered = _existing_phrases(existing_rules)

    suggestions: list[Suggestion] = []
    skipped_covered = 0
    skipped_empty_slug = 0
    for item in raw_phrases:
        phrase = str(item["phrase"]).strip()
        count = int(item["count"])
        if not phrase:
            continue
        if phrase.lower() in covered:
            skipped_covered += 1
            continue
        slug = _slugify_tag(phrase)
        if not slug:
            skipped_empty_slug += 1
            continue
        suggestions.append(
            Suggestion(
                phrase=phrase,
                count=count,
                suggested_tag=slug,
            )
        )
        if len(suggestions) >= top_n:
            break

    log.info(
        "phrase_autotag_suggest.computed",
        days=days,
        top_n=top_n,
        n_gram=n_gram,
        raw_candidates=len(raw_phrases),
        existing_rules=len(existing_rules),
        skipped_covered=skipped_covered,
        skipped_empty_slug=skipped_empty_slug,
        returned=len(suggestions),
    )
    return suggestions
