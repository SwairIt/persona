"""Per-day OCR text export as a single ``.txt`` document.

Bundles every screenshot's OCR body for a given local day into one flat
text file so the standard unix toolchain (``grep``, ``fzf``, ``awk``,
``rg``) can search the day's captured screen text without going through
the FTS5 layer.

Layout
------
Each screenshot becomes a *block*; blocks are separated by a line that
contains exactly ``===`` (triple-equals delimiter — chosen because the
sequence is highly unlikely to appear inside OCR output and it
visually scans as a section break in ``less``).

Block layout::

    <ISO timestamp>\\t<app_name>
    <OCR body — multiple lines allowed>
    <blank line>

Trailing whitespace is stripped from every body line so a ``grep -n``
hit is never visually polluted by stray spaces, and blocks with empty
OCR text still render predictably (header + blank line + delimiter).

The function intentionally returns a single ``str`` rather than
streaming bytes — the per-day window is small enough (low thousands of
shots at the absolute extreme) that buffering keeps the route, the CLI
and tests trivially testable, and the call site decides how to encode
the bytes when shipping to disk or over HTTP.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ocr_txt")

# Triple-equals on its own line is the block separator.  Kept as a
# module-level constant so callers (and tests) can import it instead of
# hard-coding the literal in their own assertions.
BLOCK_DELIMITER: str = "==="


def _parse_day(day_iso: str) -> date:
    """Validate the ``YYYY-MM-DD`` input and return a :class:`date`.

    Raises :class:`ValueError` with a human-readable message on bad
    input so route / CLI layers can translate it into a 400 or a
    non-zero exit cleanly.
    """
    try:
        return datetime.strptime(day_iso, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"invalid day {day_iso!r} (expected YYYY-MM-DD)"
        raise ValueError(msg) from exc


def _strip_trailing_ws(body: str) -> list[str]:
    """Normalise OCR text: split on any newline kind, rstrip each line.

    Returns the list of cleaned lines (may be empty for blank OCR).
    Leaves leading whitespace alone — Tesseract sometimes preserves
    column indentation that is useful for grep context.
    """
    if not body:
        return []
    # ``splitlines()`` handles ``\n``, ``\r\n`` and ``\r`` uniformly,
    # which matters because OCR running on Windows occasionally emits
    # CRLF and we don't want literal ``\r`` smuggled into the .txt.
    return [line.rstrip() for line in body.splitlines()]


def _format_block(captured_at_iso: str, app_name: str | None, ocr_text: str | None) -> str:
    """Render a single screenshot block (without the trailing delimiter).

    Header line is ``<timestamp>\\t<app_name>``.  Missing app falls back
    to a single ``-`` so the header always has two tab-separated fields
    — keeps the file parseable with ``cut -f1,2``.
    """
    app_label = app_name if app_name and app_name.strip() else "-"
    header = f"{captured_at_iso}\t{app_label}"
    body_lines = _strip_trailing_ws(ocr_text or "")
    # Header, then body, then a blank line so two consecutive blocks
    # have a visual gap before the ``===`` delimiter.
    parts: list[str] = [header, *body_lines, ""]
    return "\n".join(parts)


async def export_day_ocr_txt(day_iso: str) -> str:
    """Return the per-day OCR text dump for ``day_iso`` (``YYYY-MM-DD``).

    Walks every ``screenshots`` row whose local ``DATE(captured_at)``
    equals the requested day, ordered chronologically.  Rows with
    ``ocr_status != 'done'`` are still included — their header still
    carries useful timeline information even when the body is empty,
    and the ``===`` separator keeps grep output stable.

    Returns the concatenated string.  Empty day → empty string (the
    HTTP layer turns this into a 200 with zero content-length so naive
    ``curl > file`` keeps working).
    """
    target = _parse_day(day_iso)
    day_key = target.isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT captured_at, app_name, ocr_text "
            "FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "ORDER BY captured_at, id",
            (day_key,),
        )
        rows = list(await cursor.fetchall())

    if not rows:
        log.info("ocr_txt.export.empty", day=day_key)
        return ""

    blocks: list[str] = []
    char_total = 0
    shots_with_text = 0
    for row in rows:
        captured_at_iso = str(row["captured_at"])
        app_raw = row["app_name"]
        app_value = str(app_raw) if app_raw is not None else None
        ocr_raw = row["ocr_text"]
        ocr_value = str(ocr_raw) if ocr_raw is not None else None
        if ocr_value and ocr_value.strip():
            shots_with_text += 1
            char_total += len(ocr_value)
        blocks.append(_format_block(captured_at_iso, app_value, ocr_value))

    # ``\n===\n`` between blocks gives us the spec layout: each block
    # already ends with a blank line, so the delimiter sits on its own
    # line between blocks.  A trailing newline after the last block
    # keeps the file POSIX-conformant ("a complete line is terminated
    # by a newline") without trailing whitespace.
    separator = f"\n{BLOCK_DELIMITER}\n"
    body = separator.join(blocks)
    if not body.endswith("\n"):
        body = body + "\n"

    log.info(
        "ocr_txt.export.ok",
        day=day_key,
        shots=len(rows),
        shots_with_text=shots_with_text,
        ocr_chars=char_total,
        bytes=len(body.encode("utf-8")),
    )
    return body


def export_day_ocr_txt_sync(day_iso: str) -> str:
    """Synchronous wrapper — convenient for ``scripts/`` and quick REPL use.

    Internally drives the async coroutine with :func:`asyncio.run`, so
    it MUST NOT be called from inside an already-running event loop
    (FastAPI handlers, the CLI dispatcher, tests with
    ``asyncio_mode = 'auto'``).  Those call sites should await
    :func:`export_day_ocr_txt` directly.
    """
    return asyncio.run(export_day_ocr_txt(day_iso))


__all__ = [
    "BLOCK_DELIMITER",
    "export_day_ocr_txt",
    "export_day_ocr_txt_sync",
]
