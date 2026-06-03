"""Concatenated OCR diff between two calendar days (Persona v0.78).

Sister to :mod:`app.ocr_diff` (per-screenshot text diff) and
:mod:`app.multi_day_diff` (set/keyword diff across two days). Where the
former works one screenshot at a time and the latter rolls activity up
into apps/tags/keyword counters, this module answers a different
question: *if I pasted every OCR blob from day A end-to-end and did the
same for day B, what's the unified diff between those two firehoses?*

A single SQL round-trip per day pulls every non-empty ``ocr_text`` in
``captured_at`` order; we concatenate with blank-line separators between
screenshots so the diff hunks stay locally meaningful (a paragraph
boundary == a screenshot boundary). ``difflib.unified_diff`` is then run
exactly once over the resulting line lists.

Implementation notes:
    * The date string is validated through :func:`_parse_day` before
      reaching SQL — the only value ever interpolated is the parsed,
      canonical ISO form, and it travels as a bound parameter.
    * Per-day OCR is capped at :data:`_MAX_LINES_PER_DAY` lines so a
      runaway day (hundreds of screenshots, each with megabytes of OCR)
      can't push ``difflib`` into pathological O(n²) territory.
    * The module is async only because the DB driver is — diff math is
      stdlib-pure and runs synchronously inside the same coroutine.
"""

from __future__ import annotations

import difflib
from datetime import date
from typing import TYPE_CHECKING, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.day_ocr_diff")


# Hard cap on the concatenated per-day line count fed into difflib.
# A typical OCR blob is ~30 lines/screenshot; 8000 lines comfortably
# covers ~250 screenshots/day while keeping HtmlDiff render under a
# couple of MB.
_MAX_LINES_PER_DAY: Final[int] = 8000

# Separator inserted between adjacent screenshots' OCR text so each
# screenshot reads as its own paragraph in the diff. Two blank lines
# keep difflib's hunk grouping sane while still being obvious to a
# human skimming the unified output.
_SHOT_SEPARATOR: Final[str] = ""


def _parse_day(value: str) -> str:
    """Validate ``value`` as ``YYYY-MM-DD`` and return the canonical form.

    Re-emits via :meth:`date.isoformat` so sloppy input like ``2026-6-3``
    normalises to ``2026-06-03`` before ever reaching the
    ``DATE(captured_at) = ?`` comparison.
    """
    parsed = date.fromisoformat(value)
    return parsed.isoformat()


async def _concat_ocr_for_day(
    conn: aiosqlite.Connection,
    day_iso: str,
) -> tuple[list[str], int]:
    """Return ``(lines, shot_count)`` for the concatenated OCR of ``day_iso``.

    ``lines`` is the union of every non-empty ``ocr_text`` line from
    every screenshot captured that day, in ``captured_at`` order,
    joined by :data:`_SHOT_SEPARATOR` blank lines so screenshot
    boundaries survive into the diff. Capped at
    :data:`_MAX_LINES_PER_DAY` with a trailing truncation marker.
    """
    cursor = await conn.execute(
        "SELECT id, ocr_text FROM screenshots "
        "WHERE DATE(captured_at) = ? "
        "AND ocr_text IS NOT NULL AND ocr_text != '' "
        "ORDER BY captured_at ASC, id ASC",
        (day_iso,),
    )
    rows = await cursor.fetchall()

    lines: list[str] = []
    shot_count = 0
    for row in rows:
        shot_count += 1
        text = str(row["ocr_text"])
        if lines:
            lines.append(_SHOT_SEPARATOR)
        # A marker line per screenshot is invaluable when reading the
        # unified diff — without it a moved paragraph would look like
        # pure churn instead of a relocation.
        lines.append(f"# screenshot #{int(row['id'])}")
        lines.extend(text.splitlines())

    if len(lines) > _MAX_LINES_PER_DAY:
        truncated = lines[:_MAX_LINES_PER_DAY]
        truncated.append(
            f"# … (truncated; {len(lines) - _MAX_LINES_PER_DAY} more lines)"
        )
        return truncated, shot_count

    return lines, shot_count


async def diff_days_ocr(day_a: str, day_b: str) -> str:
    """Return the unified diff of concatenated OCR text for two days.

    Args:
        day_a: ISO date string (``YYYY-MM-DD``) for the "before" side.
        day_b: ISO date string for the "after" side.

    Returns:
        The full unified-diff text (one line per ``--- a``, ``+++ b``,
        ``@@``, ``-old``, ``+new``, context …). Empty string when the
        two days produced byte-identical concatenated OCR.

    Raises:
        ValueError: When either date string fails ISO parsing.
    """
    parsed_a = _parse_day(day_a)
    parsed_b = _parse_day(day_b)

    async with get_connection() as conn:
        lines_a, shots_a = await _concat_ocr_for_day(conn, parsed_a)
        lines_b, shots_b = await _concat_ocr_for_day(conn, parsed_b)

    unified_lines = list(
        difflib.unified_diff(
            lines_a,
            lines_b,
            fromfile=parsed_a,
            tofile=parsed_b,
            lineterm="",
        ),
    )

    log.info(
        "day_ocr_diff.computed",
        day_a=parsed_a,
        day_b=parsed_b,
        shots_a=shots_a,
        shots_b=shots_b,
        lines_a=len(lines_a),
        lines_b=len(lines_b),
        unified_lines=len(unified_lines),
    )

    return "\n".join(unified_lines)
