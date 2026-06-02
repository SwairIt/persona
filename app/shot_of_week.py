"""Screenshot-of-the-week — a curated weekly highlight picked by signals.

Unlike :mod:`app.shot_of_day`, which rotates a *random-but-stable* shot from a
broad 90-day pool, the "shot of the week" is a *signal-ranked* pick: we look
at the current ISO week (Monday → Sunday containing today), score every
captured frame by how much the user has cared about it (pinned, favourited,
tagged, annotated), and surface the top-1.

Score weights — tuned to make "explicit signals" outrank "incidental" ones:

* ``+5`` pinned   (user-marked, "never demote" tier)
* ``+3`` favourited (user-starred for quick recall)
* ``+1`` per tag (manual or auto, weak signal individually)
* ``+1`` per annotation (free-form margin scribbles)

Ties are broken by recency (``captured_at`` DESC) so a fresh-but-equal frame
beats a stale one. Within the same week the pick is therefore deterministic:
identical signals → identical winner. If no frame in the week scores above 0
*and* there are no candidates at all, we fall back to :func:`shot_of_today`
so the page never blanks out — a "this week was quiet, here's a daily pick
instead" graceful degradation.

The candidate pool is intentionally unbounded (no ``LIMIT``) because a single
ISO week is small (~tens of thousands at worst) — and we *must* see all of
it to score it correctly.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.shot_of_day import ShotOfDayPayload, shot_of_today
from app.storage.db import get_connection

log = get_logger("persona.shot_of_week")

# Public score weights — exposed as module constants so the template can
# render the breakdown ("Pinned ✓ +5") without re-hardcoding the numbers.
SCORE_PINNED = 5
SCORE_FAVOURITED = 3
SCORE_PER_TAG = 1
SCORE_PER_ANNOTATION = 1

_OCR_PREVIEW_CHARS = 200


class ShotOfWeekBreakdown(TypedDict):
    """How the winning score was assembled — surfaced verbatim in the UI."""

    pinned: bool
    favourited: bool
    tag_count: int
    annotation_count: int
    total: int


class ShotOfWeekPayload(TypedDict):
    """Public JSON / template payload.

    ``fallback`` is ``True`` when no frame in the week had any signals at all
    and we degraded to :func:`shot_of_today` — the template uses this to swap
    its subtitle from "weekly pick" to "no signals yet — daily pick instead".
    """

    id: int
    captured_at: str
    app_name: str | None
    ocr_preview: str
    score: int
    breakdown: ShotOfWeekBreakdown
    week_start: str
    week_end: str
    fallback: bool


def _current_iso_week_bounds(today: date | None = None) -> tuple[datetime, datetime]:
    """Return ``(monday_00:00, sunday_23:59:59)`` for the ISO week of ``today``.

    The boundaries are inclusive on both ends and are returned as naive
    ``datetime`` values formatted as ``YYYY-MM-DD HH:MM:SS`` — the same shape
    SQLite stores in ``captured_at`` — so a plain ``BETWEEN ? AND ?`` works
    without timezone gymnastics.

    ``today`` is injectable for tests; production passes ``None`` and we read
    the local wall clock.
    """
    anchor = today if today is not None else datetime.now().astimezone().date()
    # ``weekday()`` returns 0 for Monday, 6 for Sunday — exactly what we want.
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)
    return (
        datetime.combine(monday, time.min),
        datetime.combine(sunday, time.max),
    )


def _build_ocr_preview(ocr_text: str | None) -> str:
    """First 200 chars of OCR text, stripped, or empty string.

    Duplicated from :mod:`app.shot_of_day` rather than imported to keep the
    two modules independently evolvable — the day-shot preview length could
    diverge from the week-shot preview length later.
    """
    if not ocr_text:
        return ""
    return ocr_text.strip()[:_OCR_PREVIEW_CHARS]


def _from_day_payload(daily: ShotOfDayPayload, week_start: str, week_end: str) -> ShotOfWeekPayload:
    """Wrap a daily payload as a week payload with ``fallback=True``.

    Used when the ISO week has no signal-bearing frames. The breakdown is
    zeroed out and ``score`` is 0 — the template hides the breakdown section
    when ``fallback`` is set, so the all-zeros aren't user-visible.
    """
    return {
        "id": daily["id"],
        "captured_at": daily["captured_at"],
        "app_name": daily["app_name"],
        "ocr_preview": daily["ocr_preview"],
        "score": 0,
        "breakdown": {
            "pinned": False,
            "favourited": False,
            "tag_count": 0,
            "annotation_count": 0,
            "total": 0,
        },
        "week_start": week_start,
        "week_end": week_end,
        "fallback": True,
    }


async def shot_of_this_week() -> ShotOfWeekPayload | None:
    """Return the featured screenshot for the current ISO week.

    Algorithm:

    1. Compute the current ISO week bounds (Monday 00:00 → Sunday 23:59:59).
    2. SELECT every screenshot in that range, joined with the signal tables
       so we can score in a single query. ``LEFT JOIN`` keeps shots without
       any tags/annotations in the result.
    3. Score = ``+5`` pinned, ``+3`` favourited, ``+1`` per tag, ``+1`` per
       annotation. Tiebreak by ``captured_at DESC``.
    4. If the top score is 0 (no signal-bearing frames in the week) *or*
       there are no rows at all, fall back to :func:`shot_of_today` so the
       page renders something instead of an empty state.
    5. Return ``None`` only when both the week query and the daily fallback
       are empty — i.e. there are truly no screenshots anywhere.
    """
    week_start_dt, week_end_dt = _current_iso_week_bounds()
    # SQLite stores captured_at as ``YYYY-MM-DD HH:MM:SS`` (see
    # ``app.storage.schema``); the same lexicographic format makes BETWEEN
    # work as a date-range filter without CAST.
    week_start = week_start_dt.strftime("%Y-%m-%d %H:%M:%S")
    week_end = week_end_dt.strftime("%Y-%m-%d %H:%M:%S")
    week_start_iso = week_start_dt.date().isoformat()
    week_end_iso = week_end_dt.date().isoformat()

    # The ``COALESCE(... , 0)`` calls guarantee the score arithmetic never
    # has a NULL operand even when a screenshot has no tags or annotations.
    # We group by ``s.id`` so the COUNT()s collapse the LEFT JOIN fanout.
    query = """
        SELECT
            s.id,
            s.captured_at,
            s.app_name,
            s.ocr_text,
            (s.tier = 'pinned')                       AS is_pinned,
            (f.screenshot_id IS NOT NULL)             AS is_favourite,
            COALESCE(COUNT(DISTINCT st.tag_id), 0)    AS tag_count,
            COALESCE(COUNT(DISTINCT a.id), 0)         AS annotation_count
        FROM screenshots AS s
        LEFT JOIN favourite             AS f  ON f.screenshot_id = s.id
        LEFT JOIN screenshot_tags       AS st ON st.screenshot_id = s.id
        LEFT JOIN screenshot_annotation AS a  ON a.screenshot_id = s.id
        WHERE s.captured_at BETWEEN ? AND ?
        GROUP BY s.id
        ORDER BY (
            CASE WHEN s.tier = 'pinned' THEN ? ELSE 0 END
          + CASE WHEN f.screenshot_id IS NOT NULL THEN ? ELSE 0 END
          + COALESCE(COUNT(DISTINCT st.tag_id), 0) * ?
          + COALESCE(COUNT(DISTINCT a.id), 0) * ?
        ) DESC,
        s.captured_at DESC
        LIMIT 1
    """

    async with get_connection() as conn:
        cursor = await conn.execute(
            query,
            (
                week_start,
                week_end,
                SCORE_PINNED,
                SCORE_FAVOURITED,
                SCORE_PER_TAG,
                SCORE_PER_ANNOTATION,
            ),
        )
        row = await cursor.fetchone()

    if row is None:
        # No frames at all this week — try today's broader 90-day pool.
        log.info(
            "shot_of_week.empty_week",
            week_start=week_start_iso,
            week_end=week_end_iso,
        )
        daily = await shot_of_today()
        if daily is None:
            log.info("shot_of_week.empty_total", week_start=week_start_iso)
            return None
        return _from_day_payload(daily, week_start_iso, week_end_iso)

    pinned = bool(row["is_pinned"])
    favourited = bool(row["is_favourite"])
    tag_count = int(row["tag_count"])
    annotation_count = int(row["annotation_count"])
    score = (
        (SCORE_PINNED if pinned else 0)
        + (SCORE_FAVOURITED if favourited else 0)
        + tag_count * SCORE_PER_TAG
        + annotation_count * SCORE_PER_ANNOTATION
    )

    if score == 0:
        # Nobody cared about any frame this week. Rather than surface an
        # arbitrary "freshest" pick — which would feel arbitrary and noisy —
        # fall back to the daily shot, which is at least date-stable.
        log.info(
            "shot_of_week.zero_score_fallback",
            week_start=week_start_iso,
            candidate_id=int(row["id"]),
        )
        daily = await shot_of_today()
        if daily is None:
            return None
        return _from_day_payload(daily, week_start_iso, week_end_iso)

    payload: ShotOfWeekPayload = {
        "id": int(row["id"]),
        "captured_at": str(row["captured_at"]),
        "app_name": row["app_name"] if row["app_name"] is None else str(row["app_name"]),
        "ocr_preview": _build_ocr_preview(row["ocr_text"]),
        "score": score,
        "breakdown": {
            "pinned": pinned,
            "favourited": favourited,
            "tag_count": tag_count,
            "annotation_count": annotation_count,
            "total": score,
        },
        "week_start": week_start_iso,
        "week_end": week_end_iso,
        "fallback": False,
    }
    log.info(
        "shot_of_week.picked",
        week_start=week_start_iso,
        week_end=week_end_iso,
        picked_id=payload["id"],
        score=score,
        pinned=pinned,
        favourited=favourited,
        tag_count=tag_count,
        annotation_count=annotation_count,
    )
    return payload
