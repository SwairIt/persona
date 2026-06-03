"""CSV export of every per-screenshot annotation in the database.

v1.2 feature 2/3. Sibling of :mod:`app.web.routes.annotations_ndjson`
(v0.76, the NDJSON variant) and :mod:`app.web.routes.csv_export` /
:mod:`app.web.routes.share_visits_csv` (the wider ``/export/*.csv``
family), focussed on the ``screenshot_annotation`` table introduced in
v0.27.

``GET /export/annotations.csv`` streams the entire annotations table as
RFC-4180 CSV — one row per annotation, columns ``id``, ``shot_id``,
``body``, ``created_at``. CSV is the lingua franca for the
spreadsheet-shaped tools NDJSON consumers are happy to skip: Excel,
LibreOffice Calc, Google Sheets, pandas' ``read_csv``, ``cut -d,``,
``awk -F,``. Offering both formats lets the same dataset feed
big-data tooling (NDJSON) *and* analyst workflows (CSV) without an
intermediate conversion step.

Rows are streamed straight from the SQLite cursor via ``async for`` so
the response footprint stays O(1) in memory regardless of annotation
count — :mod:`csv` is wired through an :class:`io.StringIO` that we
flush + truncate per row, which is the canonical Python idiom for
streaming CSV without materialising the whole table. The alternative
(``fetchall()`` + ``csv.writer.writerows``) would blow up on a
million-row database.

The query is parametrised with a constant placeholder to keep the
SQL-injection convention uniform with the rest of ``app/storage`` —
even though the route takes no user input today, a future ``since`` /
``until`` filter slots in without rewriting the call site.

Note the column-name shim: the underlying table stores
``screenshot_id`` (see migration ``024_annotations.sql``), but the
public export schema spells it ``shot_id`` to match the rest of the
v0.76+ big-data exports (``annotations_ndjson``, ``sticky_export``,
``share_visits_csv``) so downstream tooling can join on a single
canonical column name.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.annotations_csv")

router = APIRouter(tags=["annotations_csv"])

# Public export column order. Mirrors the NDJSON sibling field set so
# the two formats are interchangeable for downstream tooling — load the
# CSV into pandas, load the NDJSON into DuckDB, and the schemas line up.
_CSV_COLUMNS: tuple[str, ...] = ("id", "shot_id", "body", "created_at")

# Parametrised SELECT — the ``WHERE 1 = ?`` clause is a deliberate
# placeholder so future ``since`` / ``until`` filters can be appended
# without changing the ``conn.execute(sql, params)`` call shape. The
# ``ORDER BY id ASC`` guarantees a deterministic, append-only stream
# (annotations are insert-only, so id order == created order); identical
# to :mod:`app.web.routes.annotations_ndjson` so a diff between the CSV
# and NDJSON outputs reveals serialisation bugs, not row-order drift.
_SELECT_ALL_ANNOTATIONS = (
    "SELECT id, screenshot_id, body, created_at "
    "FROM screenshot_annotation "
    "WHERE 1 = ? "
    "ORDER BY id ASC"
)


async def _iter_annotations_csv() -> AsyncIterator[bytes]:
    """Yield the header line, then one CSV row per annotation.

    The :class:`io.StringIO` buffer is reused across rows: after each
    ``writer.writerow(...)`` we grab the rendered bytes, ``yield`` them,
    then ``seek(0)`` + ``truncate(0)`` so the next row starts from an
    empty buffer. Memory stays flat regardless of annotation count —
    aiosqlite buffers a single row at a time on the SQLite side and the
    StringIO only ever holds one CSV record on the Python side.

    ``newline=""`` on the buffer keeps :mod:`csv`'s line terminator
    (default ``\\r\\n``, RFC 4180) intact instead of letting Python's
    text layer rewrite it — important because the response is binary
    bytes and downstream parsers (Excel, pandas) all accept CRLF.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)

    writer.writerow(_CSV_COLUMNS)
    header = buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)
    yield header

    streamed = 0
    async with get_connection() as conn:
        cursor = await conn.execute(_SELECT_ALL_ANNOTATIONS, (1,))
        async for row in cursor:
            writer.writerow(
                (
                    int(row["id"]),
                    int(row["screenshot_id"]),
                    str(row["body"]),
                    str(row["created_at"]),
                )
            )
            chunk = buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)
            yield chunk
            streamed += 1
        await cursor.close()
    log.info("annotations_csv.streamed", count=streamed)


@router.get("/export/annotations.csv", response_model=None)
async def export_annotations_csv() -> StreamingResponse:
    """Stream the full ``screenshot_annotation`` table as a CSV download."""
    filename = f"persona-annotations-{date.today().isoformat()}.csv"
    log.info("annotations_csv.route.start", filename=filename)
    return StreamingResponse(
        _iter_annotations_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
