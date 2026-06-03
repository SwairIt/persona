"""Per-shot OCR words TSV export — v0.90.

``GET /export/ocr-words/{shot_id}.tsv`` streams every stored Tesseract
word row for one screenshot as ``text/tab-separated-values`` so
downstream tooling (spreadsheets, awk pipelines, custom annotators)
can chew on the bounding boxes and confidence scores without speaking
HTTP-JSON.

The TSV is intentionally minimal — one header row, then one row per
word::

    word\tconf\tleft\ttop\twidth\theight
    Kubernetes\t93\t102\t44\t180\t26
    cluster\t91\t290\t44\t120\t26
    …

Columns mirror what ``ocr_word`` actually stores; missing boxes
(unboxed words from a future OCR backend) serialise as an empty
field, which is the standard TSV "null" sentinel.  Word values are
sanitised so an embedded tab or newline never breaks the row shape.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.ocr.words_tsv")

router = APIRouter(prefix="/export", tags=["ocr-words-tsv"])


_TSV_HEADER = ("word", "conf", "left", "top", "width", "height")


def _scrub(value: str) -> str:
    """Strip TSV-hostile control characters from a word value.

    Tabs and newlines would split or shift columns; carriage returns
    would confuse line-oriented consumers.  Replacing them with a
    single space keeps the row count honest while still preserving the
    visible token.
    """
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _fmt_int(value: Any) -> str:
    """Format an optional integer column.

    ``None`` (unboxed word) → empty field; anything else is coerced
    through :class:`int` so non-numeric junk surfaces here instead of
    in the streamed body.
    """
    if value is None:
        return ""
    return str(int(value))


async def _fetch_words(screenshot_id: int) -> list[dict[str, Any]]:
    """Return every ``ocr_word`` row for ``screenshot_id``, oldest first.

    Parametrised SQL — the id is bound, never interpolated.  Rows come
    back as dicts so the row-builder below stays index-free.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT word, conf, left, top, width, height "
            "FROM ocr_word "
            "WHERE screenshot_id = ? "
            "ORDER BY id ASC",
            (screenshot_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


def _render_tsv(words: list[dict[str, Any]]) -> str:
    """Render ``words`` into a TSV document with a header row."""
    buf = StringIO()
    buf.write("\t".join(_TSV_HEADER))
    buf.write("\n")
    for w in words:
        buf.write(
            "\t".join(
                (
                    _scrub(str(w["word"])),
                    _fmt_int(w["conf"]),
                    _fmt_int(w["left"]),
                    _fmt_int(w["top"]),
                    _fmt_int(w["width"]),
                    _fmt_int(w["height"]),
                ),
            ),
        )
        buf.write("\n")
    return buf.getvalue()


@router.get("/ocr-words/{shot_id}.tsv", response_model=None)
async def export_ocr_words_tsv(shot_id: int) -> StreamingResponse:
    """Stream the per-shot OCR words TSV."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        log.info("ocr.words_tsv.route.missing", shot_id=shot_id)
        raise HTTPException(status_code=404, detail="Screenshot not found")

    try:
        words = await _fetch_words(shot_id)
    except Exception:
        log.exception("ocr.words_tsv.route.failed", shot_id=shot_id)
        raise HTTPException(status_code=500, detail="OCR words TSV export failed") from None

    body = _render_tsv(words)
    payload = body.encode("utf-8")
    filename = f"persona-ocr-words-{shot_id}.tsv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info(
        "ocr.words_tsv.route.ok",
        shot_id=shot_id,
        words=len(words),
        bytes=len(payload),
    )

    return StreamingResponse(
        _iter(),
        media_type="text/tab-separated-values; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
