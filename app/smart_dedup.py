"""Trivial-change suppressor — second-pass dedup for blinking-cursor noise.

The runtime pHash deduper (``app.dedup.find_or_create_dedup_group``)
clusters shots whose perceptual hash is within a fixed Hamming distance
of an existing cluster anchor. That catches most near-identical frames
but misses a frustrating long tail: a blinking cursor, a clock tick, a
single-pixel antialias drift — visually the same scene, pHash distance
just above the deduper's ceiling, two separate rows in the timeline.

This module is the second-pass cleanup for that gap. It scans recent
screenshots and marks pairs that are:

1. From the **same** ``app_name`` (NULL on either side is treated as
   "no match" — better to skip than to over-merge cross-app frames).
2. From a **similar** ``window_title`` — first 60 chars of each must
   match exactly. Full-title equality is too brittle (clocks, unread
   counters, tab numbers leak into many titles); first-60-chars is the
   practical compromise the spec calls for.
3. pHash Hamming distance ``<= 12`` — looser than the runtime deduper
   (which typically uses 6) so we catch what it missed, tighter than
   :mod:`app.dup_finder`'s admin-finder default so we never bundle
   semantically distinct frames.
4. OCR text equivalent after stripping digits, whitespace, and
   ASCII/Unicode punctuation. That normalisation is what turns
   "10:23:14 PM" → "10:23:15 PM" + a cursor blink into a single logical
   scene.

When all four predicates hold, the **later** shot's ``trivial_dup_of_id``
gets pointed at the earlier shot's id, and the marker survives across
detector ticks because we only process rows whose marker is currently
``NULL``. The earlier ("kept") shot is left untouched.

The display-side filter (``WHERE trivial_dup_of_id IS NULL``) is **not**
applied in this tick — the shared :func:`app.storage.repository.list_screenshots`
helper feeds the timeline, corpus search, exports, audit ETL, and a
half-dozen other consumers. Adding the predicate there risks hiding
rows from places where the operator wants to see them (e.g. the exports
page). Pushing the filter into a single consumer at a time is the safer
follow-up: ship the table + detection + worker + admin UI now, layer
the timeline-only WHERE clause in a future tick once we can A/B the
visible-row count.

The module exposes:

* :func:`detect_trivial_dups` — async detector; returns counters.
* :data:`SmartDedupResult` — typed dict the detector returns.

Both are deliberately small surface area; the worker, route, and admin
UI all consume just :func:`detect_trivial_dups`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, TypedDict

from app.dedup import hamming_distance
from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.smart_dedup")

# Maximum pHash Hamming distance between the "kept" anchor and the
# candidate trivial-dup. Looser than the runtime deduper (which usually
# stops at 6) — the whole point is to catch what it missed — but
# tighter than :mod:`app.dup_finder`'s default so cross-scene collisions
# never make it into a trivial-dup bundle.
_PHASH_THRESHOLD: Final[int] = 12

# Window-title prefix length used to decide "same window". Full-title
# equality drops too many real matches because counters / clocks / tab
# numbers leak into many titles; first-60-chars is the practical balance
# the spec calls for.
_TITLE_PREFIX_LEN: Final[int] = 60

# Punctuation characters peeled from OCR text before equality check.
# We accept the ASCII punctuation block plus the common Unicode quotes
# OCR engines like to emit. Whitespace + digits get their own stripping
# pass below so they don't have to be enumerated here.
_PUNCT_CHARS: Final[frozenset[str]] = frozenset(
    [
        "!",
        '"',
        "#",
        "$",
        "%",
        "&",
        "'",
        "(",
        ")",
        "*",
        "+",
        ",",
        "-",
        ".",
        "/",
        ":",
        ";",
        "<",
        "=",
        ">",
        "?",
        "@",
        "[",
        "\\",
        "]",
        "^",
        "_",
        "`",
        "{",
        "|",
        "}",
        "~",
        "«",
        "»",
        "“",
        "”",
        "‘",  # noqa: RUF001 — typographic single quote
        "’",  # noqa: RUF001 — typographic single quote
        "—",
        "–",  # noqa: RUF001 — en dash, OCR-emitted alternative to hyphen
        "…",
    ]
)


class SmartDedupResult(TypedDict):
    """Counters returned by :func:`detect_trivial_dups`.

    ``scanned`` is the number of rows pulled from the DB in the
    lookback window; ``marked`` is how many of them we wrote a
    ``trivial_dup_of_id`` to; ``kept`` is how many of the scanned rows
    were left alone (either because they are the "anchor" of a bundle
    or because no adjacent neighbour qualified).
    """

    scanned: int
    marked: int
    kept: int


def _normalise_ocr(text: str | None) -> str:
    """Return ``text`` with digits, whitespace, and punctuation removed.

    Two OCR strings that differ only by a clock tick or a cursor-driven
    re-render should collapse to the same normalised form here. Empty
    input or a value made entirely of stripped characters both collapse
    to ``""`` — the caller treats that as "no useful text", which means
    pHash + window-title are the only equivalence signals left and
    we'll be more conservative downstream.
    """
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ch.isdigit():
            continue
        if ch.isspace():
            continue
        if ch in _PUNCT_CHARS:
            continue
        out.append(ch.casefold())
    return "".join(out)


def _title_prefix(title: str | None) -> str:
    """Return the first :data:`_TITLE_PREFIX_LEN` chars of ``title``.

    ``None`` and empty strings collapse to ``""``; the caller refuses to
    pair two empty-title rows so an untitled window can never anchor a
    bundle.
    """
    if not title:
        return ""
    return title[:_TITLE_PREFIX_LEN]


def _phash_close(left: str | None, right: str | None) -> bool:
    """``True`` when both pHashes are present and within the threshold.

    Missing values on either side mean we have no similarity signal so
    we conservatively refuse. ``hamming_distance`` raises on mixed-
    length pHashes (legacy rows with a different ``hash_size``); we
    swallow that as "not close" rather than letting one bad row poison
    the whole tick.
    """
    if not left or not right:
        return False
    try:
        return hamming_distance(left, right) <= _PHASH_THRESHOLD
    except ValueError:
        return False


def _is_trivial_pair(
    earlier: aiosqlite.Row,
    later: aiosqlite.Row,
) -> bool:
    """Decide whether ``later`` is a trivial dup of ``earlier``.

    All four predicates from the module docstring have to hold:
    same app, similar window prefix, pHash within threshold, OCR text
    equivalent after stripping digits/whitespace/punctuation.

    A pair fails closed — i.e. any missing signal returns ``False`` —
    so an unparseable row can never accidentally get bundled.
    """
    earlier_app = earlier["app_name"]
    later_app = later["app_name"]
    if earlier_app is None or later_app is None:
        return False
    if str(earlier_app) != str(later_app):
        return False

    earlier_title = _title_prefix(
        None if earlier["window_title"] is None else str(earlier["window_title"])
    )
    later_title = _title_prefix(
        None if later["window_title"] is None else str(later["window_title"])
    )
    if not earlier_title or earlier_title != later_title:
        return False

    earlier_phash = None if earlier["phash"] is None else str(earlier["phash"])
    later_phash = None if later["phash"] is None else str(later["phash"])
    if not _phash_close(earlier_phash, later_phash):
        return False

    earlier_text = _normalise_ocr(
        None if earlier["ocr_text"] is None else str(earlier["ocr_text"])
    )
    later_text = _normalise_ocr(
        None if later["ocr_text"] is None else str(later["ocr_text"])
    )
    # When both stripped texts are empty we still bundle — the pHash
    # similarity plus identical window-title prefix is enough signal
    # for a "no visible text" scene like a movie playback frame.
    return earlier_text == later_text


async def detect_trivial_dups(lookback_hours: int = 6) -> SmartDedupResult:
    """Scan recent shots and mark trivial dupes pointing at the earlier sibling.

    Args:
        lookback_hours: How far back to consider. Six hours is enough
            cadence-wise for the 1800-second worker poll and bounds the
            scan to a few thousand rows on a busy machine.

    Returns:
        :class:`SmartDedupResult` with three counters — see the type
        docstring for the precise definition of each.

    Behaviour:

    * Only rows whose ``trivial_dup_of_id IS NULL`` are scanned, so
      re-runs of the detector on the same window are idempotent.
    * Soft-deleted rows (``deleted_at IS NOT NULL``) are skipped — the
      operator already decided those are unwanted; re-pointing them at
      an anchor would be confusing.
    * The detector walks adjacent rows in ``captured_at`` ASC order.
      When a pair qualifies, ``later`` is marked as a dup of ``earlier``
      and the walking pointer advances; the same ``earlier`` can anchor
      multiple following dups in a row (a long blinking-cursor streak
      collapses to a single kept shot).
    """
    if lookback_hours <= 0:
        msg = f"lookback_hours must be > 0, got {lookback_hours}"
        raise ValueError(msg)

    window_start = datetime.now(tz=UTC) - timedelta(hours=lookback_hours)

    scanned = 0
    marked = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title, phash, ocr_text "
            "FROM screenshots "
            "WHERE captured_at >= ? "
            "  AND trivial_dup_of_id IS NULL "
            "  AND deleted_at IS NULL "
            "ORDER BY captured_at ASC",
            (window_start.isoformat(),),
        )
        rows = list(await cursor.fetchall())
        scanned = len(rows)

        if scanned < 2:
            log.info(
                "smart_dedup.tick",
                lookback_hours=lookback_hours,
                scanned=scanned,
                marked=0,
                kept=scanned,
            )
            return {"scanned": scanned, "marked": 0, "kept": scanned}

        # ``anchor`` is the most recent shot we decided to keep. Each
        # subsequent row is compared against it; on a match the row
        # gets pointed at the anchor and the anchor stays put. On a
        # miss the row becomes the new anchor.
        anchor = rows[0]
        for later in rows[1:]:
            if _is_trivial_pair(anchor, later):
                await conn.execute(
                    "UPDATE screenshots "
                    "SET trivial_dup_of_id = ? "
                    "WHERE id = ? AND trivial_dup_of_id IS NULL",
                    (int(anchor["id"]), int(later["id"])),
                )
                marked += 1
                continue
            anchor = later

        await conn.commit()

    kept = scanned - marked
    log.info(
        "smart_dedup.tick",
        lookback_hours=lookback_hours,
        scanned=scanned,
        marked=marked,
        kept=kept,
    )
    return {"scanned": scanned, "marked": marked, "kept": kept}


__all__ = ["SmartDedupResult", "detect_trivial_dups"]
