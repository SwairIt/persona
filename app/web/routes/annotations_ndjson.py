"""NDJSON export of every per-screenshot annotation in the database.

v0.76 feature 1/3. Sibling of :mod:`app.web.routes.sticky_export` and
:mod:`app.web.routes.csv_export`, focussed on the ``screenshot_annotation``
table introduced in v0.27.

``GET /export/annotations.ndjson`` streams the entire annotations table as
newline-delimited JSON — one row per line, columns ``id``, ``shot_id``,
``body``, ``created_at``. NDJSON (a.k.a. ``application/x-ndjson``) is the
format of choice for big-data tooling (jq -c, DuckDB ``read_ndjson``,
ClickHouse ``JSONEachRow``, BigQuery ingest) because each line is a
self-contained JSON object — consumers can mmap, ``head``, ``tail``, or
``grep`` the file without parsing the whole document.

Rows are streamed straight from the SQLite cursor via ``async for`` so
the response footprint stays O(1) in memory regardless of annotation
count — the alternative (``fetchall()`` + ``json.dumps``) would blow up
on a million-row database. The query is parametrised with a constant
placeholder to keep the SQL-injection convention uniform with the rest
of ``app/storage`` — even though the route takes no user input today, a
future ``since`` / ``until`` filter slots in without rewriting the call
site.

Note the column-name shim: the underlying table stores
``screenshot_id`` (see migration ``024_annotations.sql``), but the public
export schema spells it ``shot_id`` to match the rest of the v0.76
big-data exports (``sticky_export``, ``share_visits_csv``) so downstream
tooling can join on a single canonical column name.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.annotations_ndjson")

router = APIRouter(tags=["annotations_ndjson"])


# Parametrised SELECT — the ``WHERE 1 = ?`` clause is a deliberate
# placeholder so future ``since`` / ``until`` filters can be appended
# without changing the ``conn.execute(sql, params)`` call shape. The
# ``ORDER BY id ASC`` guarantees a deterministic, append-only stream
# (annotations are insert-only, so id order == created order).
_SELECT_ALL_ANNOTATIONS = (
    "SELECT id, screenshot_id, body, created_at "
    "FROM screenshot_annotation "
    "WHERE 1 = ? "
    "ORDER BY id ASC"
)


async def _iter_annotations_ndjson() -> AsyncIterator[bytes]:
    """Yield one ``\\n``-terminated JSON object per annotation row.

    The cursor is iterated lazily with ``async for`` so memory stays
    flat — aiosqlite buffers a single row at a time, not the whole
    result set. ``ensure_ascii=False`` keeps Unicode bodies (Cyrillic,
    emoji, CJK) compact instead of bloating them into ``\\uXXXX``
    escapes; the response is explicitly UTF-8.
    """
    streamed = 0
    async with get_connection() as conn:
        cursor = await conn.execute(_SELECT_ALL_ANNOTATIONS, (1,))
        async for row in cursor:
            obj = {
                "id": int(row["id"]),
                "shot_id": int(row["screenshot_id"]),
                "body": str(row["body"]),
                "created_at": str(row["created_at"]),
            }
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            yield (line + "\n").encode("utf-8")
            streamed += 1
        await cursor.close()
    log.info("annotations_ndjson.streamed", count=streamed)


@router.get("/export/annotations.ndjson", response_model=None)
async def export_annotations_ndjson() -> StreamingResponse:
    """Stream the full ``screenshot_annotation`` table as NDJSON download."""
    filename = f"persona-annotations-{date.today().isoformat()}.ndjson"
    log.info("annotations_ndjson.route.start", filename=filename)
    return StreamingResponse(
        _iter_annotations_ndjson(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
