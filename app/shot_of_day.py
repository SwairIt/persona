"""Screenshot-of-the-day — daily-rotating "featured" shot picked deterministically.

The selection is stable for a given calendar day (so refreshing the page yields
the same shot all day) but rolls over to a different shot the next day. We seed
a SHA-256 hash with today's ISO date — Python's built-in :func:`hash` is salted
across interpreter runs and would therefore re-pick on every restart, which is
why a hashlib digest is used instead.

The candidate pool is intentionally bounded (last 90 days, ``LIMIT 5000``) so a
huge ``screenshots`` table cannot blow up memory or wall-clock time.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shot_of_day")

_CANDIDATE_WINDOW_DAYS = 90
_CANDIDATE_LIMIT = 5000
_OCR_PREVIEW_CHARS = 200


class ShotOfDayPayload(TypedDict):
    id: int
    captured_at: str
    app_name: str | None
    ocr_preview: str


def _stable_seed(iso_date: str) -> int:
    """Return a stable 64-bit unsigned int derived from ``iso_date``.

    Python's :func:`hash` is salted per-process, so it would re-pick the
    featured shot on every server restart. SHA-256 is overkill cryptographically
    but it's the cheapest stdlib way to get a stable, well-distributed digest.
    """
    digest = hashlib.sha256(iso_date.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _build_ocr_preview(ocr_text: str | None) -> str:
    """First 200 chars of OCR text, stripped, or empty string."""
    if not ocr_text:
        return ""
    return ocr_text.strip()[:_OCR_PREVIEW_CHARS]


async def shot_of_today() -> ShotOfDayPayload | None:
    """Return the featured screenshot for today, or ``None`` if no candidates.

    Algorithm:

    1. Build a stable seed from today's ISO date via SHA-256.
    2. Pull up to ``_CANDIDATE_LIMIT`` recent screenshot ids (last 90 days),
       ordered by id so the candidate list is itself deterministic.
    3. Pick ``candidates[seed % len(candidates)]`` and fetch its row.
    4. Return a small public dict with an ``ocr_preview`` (first 200 chars).
    """
    today_iso = datetime.now().astimezone().date().isoformat()
    seed = _stable_seed(today_iso)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM screenshots "
            "WHERE captured_at >= datetime('now', ?) "
            "ORDER BY id "
            "LIMIT ?",
            (f"-{_CANDIDATE_WINDOW_DAYS} days", _CANDIDATE_LIMIT),
        )
        id_rows = await cursor.fetchall()

        candidate_ids: list[int] = [int(row["id"]) for row in id_rows]
        if not candidate_ids:
            log.info("shot_of_day.empty", today=today_iso)
            return None

        picked_id = candidate_ids[seed % len(candidate_ids)]

        detail_cursor = await conn.execute(
            "SELECT id, captured_at, app_name, ocr_text "
            "FROM screenshots WHERE id = ?",
            (picked_id,),
        )
        row = await detail_cursor.fetchone()

    if row is None:
        # Race: the picked row was deleted between the two queries. Treat as
        # empty rather than crash — the caller can render the empty state and
        # the next refresh will recompute.
        log.warning("shot_of_day.picked_missing", picked_id=picked_id, today=today_iso)
        return None

    ocr_preview = _build_ocr_preview(row["ocr_text"])
    payload: ShotOfDayPayload = {
        "id": int(row["id"]),
        "captured_at": str(row["captured_at"]),
        "app_name": row["app_name"] if row["app_name"] is None else str(row["app_name"]),
        "ocr_preview": ocr_preview,
    }
    log.info(
        "shot_of_day.picked",
        today=today_iso,
        picked_id=payload["id"],
        candidates=len(candidate_ids),
        app_name=payload["app_name"],
    )
    return payload
