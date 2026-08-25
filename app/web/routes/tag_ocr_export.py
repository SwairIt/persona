"""Per-tag OCR full-text export — every OCR body for shots carrying a tag.

``GET /export/tag/{tag}/ocr.txt`` streams a flat ``text/plain`` dump of
every screenshot's OCR text for shots that carry the requested tag. The
shape mirrors the per-day OCR export (:mod:`app.ocr_txt_export`) so the
same unix toolchain (``grep``, ``fzf``, ``awk``, ``rg``) works on either
file with no per-format awareness.

Layout
------
One block per screenshot, blocks separated by a line that contains
exactly ``===`` (triple-equals delimiter — matches the per-day file so
downstream parsers stay format-agnostic).

Block layout::

    <shot_id>\\t<captured_at_iso>\\t<app_name>
    <OCR body — multiple lines allowed>
    <blank line>

Trailing whitespace is stripped from every body line so ``grep -n``
hits never carry stray spaces. Missing ``app_name`` becomes ``-`` so
the header always has three tab-separated fields, keeping the file
parseable with ``cut -f1,2,3``.

The route streams the result with ``Content-Disposition: attachment``
and a tag-stamped filename, plain UTF-8, plus ``Cache-Control:
no-store`` to keep stale grep targets out of browser caches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterator

    import aiosqlite

log = get_logger("persona.tag_ocr_export")

router = APIRouter(prefix="/export", tags=["tag-ocr-export"])

# Triple-equals on its own line is the block separator — kept identical
# to :data:`app.ocr_txt_export.BLOCK_DELIMITER` so parsers don't need a
# per-export-flavour switch.
BLOCK_DELIMITER: str = "==="


def _strip_trailing_ws(body: str) -> list[str]:
    """Normalise OCR text: split on any newline kind, rstrip each line.

    Returns the list of cleaned lines (may be empty for blank OCR).
    Leaves leading whitespace alone — Tesseract sometimes preserves
    column indentation that is useful for grep context.
    """
    if not body:
        return []
    # ``splitlines()`` handles ``\n``, ``\r\n`` and ``\r`` uniformly so
    # OCR running on Windows can't smuggle literal ``\r`` into the file.
    return [line.rstrip() for line in body.splitlines()]


def _format_block(
    shot_id: int,
    captured_at_iso: str,
    app_name: str | None,
    ocr_text: str | None,
) -> str:
    """Render a single screenshot block (without the trailing delimiter).

    Header line is ``<shot_id>\\t<captured_at>\\t<app_name>``. Missing
    app falls back to a single ``-`` so the header always carries three
    tab-separated fields — keeps the file parseable with ``cut``.
    """
    app_label = app_name if app_name and app_name.strip() else "-"
    header = f"{shot_id}\t{captured_at_iso}\t{app_label}"
    body_lines = _strip_trailing_ws(ocr_text or "")
    # Header, then body, then a blank line so two consecutive blocks
    # have a visual gap before the ``===`` delimiter.
    parts: list[str] = [header, *body_lines, ""]
    return "\n".join(parts)


async def _lookup_tag(conn: aiosqlite.Connection, name: str) -> dict[str, Any] | None:
    """Resolve a tag by its case-insensitive ``name``.

    Returns ``None`` when the tag does not exist so the caller can map
    that into a 404. ``name`` is bound through a parameter placeholder
    so a tag like ``%`` cannot smuggle a wildcard into the ``=``
    comparison.
    """
    cursor = await conn.execute(
        "SELECT id, name FROM tags WHERE name = ?",
        (name.strip().lower(),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {"id": int(row["id"]), "name": str(row["name"])}


async def _fetch_tagged_shots(
    conn: aiosqlite.Connection,
    tag_id: int,
) -> list[dict[str, Any]]:
    """Return every shot carrying ``tag_id`` in chronological order.

    Only the columns we render (``id``, ``captured_at``, ``app_name``,
    ``ocr_text``) are selected — we never materialise the full row so
    the OCR blob is the only heavy column we pay for. Ordering is
    ``captured_at, id`` so two shots in the same second still produce
    a deterministic block order across runs.
    """
    cursor = await conn.execute(
        """
        SELECT s.id           AS id,
               s.captured_at  AS captured_at,
               s.app_name     AS app_name,
               s.ocr_text     AS ocr_text
        FROM screenshots s
        JOIN screenshot_tags st ON st.screenshot_id = s.id
        WHERE st.tag_id = ?
        ORDER BY s.captured_at, s.id
        """,
        (tag_id,),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _build_tag_ocr_dump(tag_name: str) -> tuple[dict[str, Any], str]:
    """Resolve ``tag_name`` and render the flat OCR dump for its shots.

    Returns ``(tag_row, body)``. The body is an empty string when the
    tag exists but carries no shots — the HTTP layer turns that into a
    200 with zero content-length so naive ``curl > file`` still works.
    Raises :class:`LookupError` when the tag does not exist so the
    route can map it to a 404.
    """
    async with get_connection() as conn:
        tag_row = await _lookup_tag(conn, tag_name)
        if tag_row is None:
            msg = f"Tag not found: {tag_name}"
            raise LookupError(msg)
        rows = await _fetch_tagged_shots(conn, tag_row["id"])

    if not rows:
        log.info(
            "tag_ocr_export.empty",
            tag=tag_row["name"],
            tag_id=tag_row["id"],
        )
        return tag_row, ""

    blocks: list[str] = []
    char_total = 0
    shots_with_text = 0
    for row in rows:
        shot_id = int(row["id"])
        captured_at_iso = str(row["captured_at"])
        app_raw = row["app_name"]
        app_value = str(app_raw) if app_raw is not None else None
        ocr_raw = row["ocr_text"]
        ocr_value = str(ocr_raw) if ocr_raw is not None else None
        if ocr_value and ocr_value.strip():
            shots_with_text += 1
            char_total += len(ocr_value)
        blocks.append(_format_block(shot_id, captured_at_iso, app_value, ocr_value))

    # ``\n===\n`` between blocks gives us the spec layout: each block
    # already ends with a blank line, so the delimiter sits on its own
    # line between blocks. A trailing newline after the last block
    # keeps the file POSIX-conformant.
    separator = f"\n{BLOCK_DELIMITER}\n"
    body = separator.join(blocks)
    if not body.endswith("\n"):
        body = body + "\n"

    log.info(
        "tag_ocr_export.ok",
        tag=tag_row["name"],
        tag_id=tag_row["id"],
        shots=len(rows),
        shots_with_text=shots_with_text,
        ocr_chars=char_total,
        bytes=len(body.encode("utf-8")),
    )
    return tag_row, body


def _safe_filename_slug(name: str) -> str:
    """Build an ASCII-safe filename slug from a tag name.

    The slug is only used inside the ``Content-Disposition`` filename;
    non-ASCII characters and shell metacharacters are replaced with
    ``_`` so the suggested download name is portable across browsers
    and shells. Empty result falls back to ``tag`` so we never emit
    ``persona-tag-ocr-.txt``.

    ``str.isalnum()`` is True for Cyrillic, CJK and every other unicode
    letter, so the original filter let non-ASCII straight through into the
    plain ``filename="…"`` parameter. HTTP header values are latin-1, so any
    Russian tag — the common case on this instance — turned this endpoint
    into a hard 500 (``UnicodeEncodeError``) at response-send time, after the
    export body had already been built. ASCII is checked explicitly; the
    readable name still travels in the RFC 5987 ``filename*`` parameter.
    """
    cleaned_chars = [
        c if ((c.isascii() and c.isalnum()) or c in "-_") else "_" for c in name
    ]
    slug = "".join(cleaned_chars).strip("_")
    return slug or "tag"


@router.get("/tag/{tag}/ocr.txt", response_model=None)
async def export_tag_ocr_txt(tag: str) -> StreamingResponse:
    """Stream the per-tag OCR text dump for ``tag``."""
    try:
        tag_row, body = await _build_tag_ocr_dump(tag)
    except LookupError as exc:
        log.info("tag_ocr_export.route.missing", tag=tag)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception:
        log.exception("tag_ocr_export.route.failed", tag=tag)
        raise HTTPException(
            status_code=500, detail="Tag OCR text export failed"
        ) from None

    payload = body.encode("utf-8")
    slug = _safe_filename_slug(str(tag_row["name"]))
    filename = f"persona-tag-ocr-{slug}.txt"
    # RFC 5987 ``filename*`` carries the original (possibly non-ASCII)
    # tag name so download managers that honour the extended form get
    # the readable label, while ``filename=`` keeps a legacy-safe slug.
    encoded_name = quote(str(tag_row["name"]), safe="")
    disposition = (
        f'attachment; filename="{filename}"; '
        f"filename*=UTF-8''persona-tag-ocr-{encoded_name}.txt"
    )

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info(
        "tag_ocr_export.route.ok",
        tag=tag_row["name"],
        tag_id=tag_row["id"],
        bytes=len(payload),
    )

    return StreamingResponse(
        _iter(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = [
    "BLOCK_DELIMITER",
    "_build_tag_ocr_dump",
    "router",
]
