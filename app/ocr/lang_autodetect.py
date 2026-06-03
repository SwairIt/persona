"""OCR language auto-detect — per-app Tesseract language pack recommender.

The user picks Tesseract language packs globally in :mod:`app.ocr.languages`
(``ocr_languages`` kv row, ``+``-joined). That works fine when every screen
on the host is in the same script, but the moment a user spends half their
day in Russian-language IDEs and half in a Chinese chat client, a single
global pack list either over-fetches CPU (every pass tries every pack) or
silently mis-OCRs whichever app drifts out of the configured set.

This module **does not** rewrite the global setting on its own. It samples
the most recent ``ocr_text`` rows per app, classifies their character
content using the v0.39 ``language_stats`` rules, and returns a *list of
recommended pack names per app*. The UI layer surfaces those
recommendations and lets the operator one-click apply them — by widening
the global ``ocr_languages`` to the union of every recommendation plus the
current selection.

The classifier is intentionally script-based (Cyrillic / Latin / CJK)
rather than language-based: Tesseract packs are themselves script-tied
(``eng`` / ``rus`` / ``chi_sim``), and a Unicode-range count is cheap,
predictable and dependency-free.
"""

from __future__ import annotations

from collections import Counter
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.ocr.language_stats import _classify
from app.storage.db import get_connection

log = get_logger("persona.ocr.lang_autodetect")


# How many of the most-recent rows per app the sampler reads. The whole
# point is a quick heuristic — counting fifty windows-worth of text per
# app keeps the SQL light even on a host with hundreds of distinct
# ``app_name`` values, while still giving enough signal for the
# percentages below to stabilise.
_SHOTS_PER_APP: Final[int] = 50

# Hard cap on the number of distinct apps inspected per call. A user with
# thousands of unique ``app_name`` rows from years of capture should not
# see one click on the page hammer SQLite for minutes.
_MAX_APPS: Final[int] = 500

# Minimum total chars per app before we emit any recommendation. Below
# this floor the percentages are noise — an app with only "OK" / "Yes"
# strings would otherwise flip its recommendation on every fresh capture.
_MIN_CHARS_FOR_RECOMMENDATION: Final[int] = 40

# Percentage-of-total threshold a script must cross before its matching
# Tesseract pack is recommended. A single window often mixes scripts (a
# Russian IDE with English code, a Chinese chat with Latin URLs); a 50%
# floor picks the one or two scripts that actually dominate the corpus
# rather than emitting a four-pack laundry list for every app.
_DOMINANCE_THRESHOLD_PCT: Final[float] = 50.0

# Mapping from internal script bucket to canonical Tesseract pack name.
# Digits and "other" never trigger a recommendation — they're not a
# script Tesseract has a pack for. Order in this tuple is the rendering
# order of recommendations, so users see ``eng`` before ``rus`` before
# ``chi_sim`` in the UI — easier to scan when sorted alphabetically by
# pack name.
_SCRIPT_TO_PACK: Final[tuple[tuple[str, str], ...]] = (
    ("cjk", "chi_sim"),
    ("cyrillic", "rus"),
    ("latin", "eng"),
)


class AppRecommendation(TypedDict):
    """One row of :func:`recommend_languages_per_app`.

    ``recommended`` is the list of Tesseract pack names whose
    corresponding script crossed :data:`_DOMINANCE_THRESHOLD_PCT` in the
    sampled window. ``script_percentages`` is the full breakdown so the
    UI can show *why* a pack was suggested without re-running the scan.
    ``total_chars`` and ``shots_sampled`` are echoed back so the operator
    can spot apps where the recommendation is based on too little data.
    """

    app_name: str
    shots_sampled: int
    total_chars: int
    script_percentages: dict[str, float]
    recommended: list[str]


def _percentages(counts: Counter[str], total: int) -> dict[str, float]:
    """Return per-bucket percentages, with one decimal place precision.

    ``total`` is passed explicitly rather than recomputed from ``counts``
    because the caller already knows it and division by zero must produce
    a zeroed dict — never a ``ZeroDivisionError``.
    """
    if total <= 0:
        return {bucket: 0.0 for bucket in ("cyrillic", "latin", "cjk", "digit", "other")}
    return {
        bucket: round(counts.get(bucket, 0) / total * 100.0, 1)
        for bucket in ("cyrillic", "latin", "cjk", "digit", "other")
    }


def _recommend_for_app(counts: Counter[str], total: int) -> list[str]:
    """Pick Tesseract packs whose script crossed the dominance threshold.

    Returns an empty list when ``total`` is below the minimum-evidence
    floor — the UI then displays a "not enough text yet" hint rather
    than a recommendation derived from a handful of glyphs.
    """
    if total < _MIN_CHARS_FOR_RECOMMENDATION:
        return []
    recommended: list[str] = []
    for script, pack in _SCRIPT_TO_PACK:
        percent = counts.get(script, 0) / total * 100.0
        if percent >= _DOMINANCE_THRESHOLD_PCT:
            recommended.append(pack)
    return recommended


async def recommend_languages_per_app() -> dict[str, list[str]]:
    """Return ``{app_name: [recommended Tesseract packs]}``.

    Samples the most recent :data:`_SHOTS_PER_APP` OCR rows per app
    (capped at :data:`_MAX_APPS` distinct apps), classifies each
    character via the v0.39 :func:`app.ocr.language_stats._classify`
    helper, and recommends a pack whenever its script crosses
    :data:`_DOMINANCE_THRESHOLD_PCT` of the sampled corpus.

    Apps with no recommendation are still present in the returned dict
    with an empty list — the UI distinguishes "scanned, nothing
    dominant" from "never scanned" by checking key presence.
    """
    detail = await _collect_details()
    return {entry["app_name"]: entry["recommended"] for entry in detail}


async def recommend_languages_detailed() -> list[AppRecommendation]:
    """Return per-app rows with full breakdown for the admin page.

    Same scan as :func:`recommend_languages_per_app` but each row
    carries the percentages, sampled-shot count and total char count so
    the table can show evidence alongside each recommendation. Sorted
    by ``total_chars`` descending — high-traffic apps land on top
    where the operator is most likely to act.
    """
    detail = await _collect_details()
    detail.sort(key=lambda entry: entry["total_chars"], reverse=True)
    return detail


async def _collect_details() -> list[AppRecommendation]:
    """Walk SQLite, classify per-app ``ocr_text`` and build the row list.

    The inner SELECT uses a window function (``ROW_NUMBER() OVER ... ORDER
    BY captured_at DESC``) to keep only the latest :data:`_SHOTS_PER_APP`
    rows per ``app_name``. SQLite 3.25+ ships window functions; the
    project's minimum supported runtime (Python 3.12 / aiosqlite 0.20+)
    bundles a newer SQLite than that, so no fallback is needed.

    The outer LIMIT applies to *apps*, not rows — implemented in Python
    so a malformed ``app_name`` column on disk can't break it.
    """
    per_app_counts: dict[str, Counter[str]] = {}
    per_app_shots: dict[str, int] = {}

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT app_name, ocr_text
            FROM (
                SELECT
                    app_name,
                    ocr_text,
                    ROW_NUMBER() OVER (
                        PARTITION BY app_name
                        ORDER BY captured_at DESC
                    ) AS rn
                FROM screenshots
                WHERE app_name IS NOT NULL
                  AND app_name != ''
                  AND ocr_text IS NOT NULL
                  AND ocr_text != ''
            )
            WHERE rn <= ?
            """,
            (_SHOTS_PER_APP,),
        )
        async for row in cursor:
            app_name = str(row["app_name"])
            text = str(row["ocr_text"])
            counts = per_app_counts.setdefault(app_name, Counter())
            for ch in text:
                counts[_classify(ch)] += 1
            per_app_shots[app_name] = per_app_shots.get(app_name, 0) + 1

    # Sort + cap deterministically so two calls on the same DB return the
    # same ordering — important for snapshot-style tests.
    ranked_apps = sorted(per_app_counts.keys())[:_MAX_APPS]

    detail: list[AppRecommendation] = []
    for app_name in ranked_apps:
        counts = per_app_counts[app_name]
        total = sum(counts.values())
        detail.append(
            {
                "app_name": app_name,
                "shots_sampled": per_app_shots.get(app_name, 0),
                "total_chars": total,
                "script_percentages": _percentages(counts, total),
                "recommended": _recommend_for_app(counts, total),
            }
        )

    log.info(
        "ocr.lang_autodetect.scanned",
        apps_scanned=len(detail),
        apps_with_recommendation=sum(1 for entry in detail if entry["recommended"]),
        shots_per_app_cap=_SHOTS_PER_APP,
    )
    return detail


__all__ = [
    "AppRecommendation",
    "recommend_languages_detailed",
    "recommend_languages_per_app",
]
