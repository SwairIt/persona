"""HTTP route for the v1.7 per-app shots CSV export.

``GET /export/app/{app_name}/shots.csv`` streams a ``text/csv`` document
with one row per screenshot captured under the given ``app_name``. The
endpoint is the dump-everything sibling of the ``/apps/{name}`` HTML
detail page (:mod:`app.web.routes.app_stats`) — instead of an
aggregated view, callers get the raw row list for spreadsheet drill-down
or ad-hoc analysis offline.

Columns (in order):
    id                 — integer primary key from ``screenshots.id``.
    captured_at        — UTC ISO-8601 string as stored on the row.
    dominant_script    — bucket label from migration 084 (``cyrillic`` /
                         ``latin`` / ``cjk`` / ``digit`` / ``other``).
                         Empty string when the OCR worker hasn't tagged
                         the shot yet (NULL in the column).
    ocr_length         — ``LENGTH(ocr_text)`` coalesced to ``0`` for rows
                         that haven't been OCR'd. Mirrors the field the
                         search route exposes as a sort key (see
                         :mod:`app.web.routes.search`).

Streaming via :class:`fastapi.responses.StreamingResponse` (single
chunk) mirrors :mod:`app.web.routes.stats_csv` and
:mod:`app.web.routes.share_visits_csv` — keeps the
``Content-Disposition`` filename header authoritative and lets browsers
download-rather-than-render the payload regardless of the inferred
media type.

App-name match is an exact equality, parametrised. An empty result set
returns ``404 Not Found`` so a typo doesn't silently hand the caller a
header-only CSV file. The renderer is split out from the route so the
``export-app-shots-csv`` CLI subcommand in :mod:`app.cli` can reuse the
exact same query + serialisation without going through FastAPI.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.app_shots_csv")

router = APIRouter(prefix="/export", tags=["app-shots-csv"])

_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "captured_at",
    "dominant_script",
    "ocr_length",
)


class AppNotFoundError(LookupError):
    """No screenshots exist for the requested ``app_name``."""


def _safe_filename(app_name: str) -> str:
    """Build a filesystem-friendly ``Content-Disposition`` filename.

    Strips characters that break HTTP header quoting or filesystem
    naming on Windows (``" \\ / : * ? < > |``) and falls back to a
    constant slug if everything is filtered out. The full app name is
    still authoritative in the URL and in the log line — this is only
    the suggested save-as name.
    """
    forbidden = set('"\\/:*?<>|\r\n\t')
    cleaned = "".join("_" if ch in forbidden else ch for ch in app_name).strip()
    return cleaned or "app"


@router.get("/app/{app_name}/shots.csv", response_model=None)
async def export_app_shots_csv(app_name: str) -> StreamingResponse:
    """Stream every screenshot row recorded under ``app_name`` as CSV."""
    try:
        body = await _render_app_shots_csv(app_name=app_name)
    except AppNotFoundError as exc:
        log.info("app_shots_csv.route.not_found", app_name=app_name)
        raise HTTPException(
            status_code=404,
            detail=f"App not found: {app_name}",
        ) from exc
    except Exception:
        log.exception("app_shots_csv.route.failed", app_name=app_name)
        raise HTTPException(
            status_code=500,
            detail="per-app shots CSV export failed",
        ) from None

    payload = body.encode("utf-8")
    filename = f"persona-app-{_safe_filename(app_name)}-shots.csv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info(
        "app_shots_csv.route.ok",
        app_name=app_name,
        bytes=len(payload),
    )

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


async def _render_app_shots_csv(*, app_name: str) -> str:
    """Read the ``screenshots`` table and return the CSV body as a string.

    Raises :class:`AppNotFoundError` when no rows match — the route maps
    that to ``404`` and the CLI maps it to a non-zero exit code with a
    helpful stderr message, so the two surfaces agree on what "empty"
    means without either of them silently emitting a header-only CSV.

    Parametrised SQL only — the ``app_name`` flows in as a bound
    parameter so we never interpolate user input into the query string.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)

    rows_written = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                id,
                captured_at,
                dominant_script,
                COALESCE(LENGTH(ocr_text), 0) AS ocr_length
            FROM screenshots
            WHERE app_name = ?
            ORDER BY captured_at ASC, id ASC
            """,
            (app_name,),
        )
        async for row in cursor:
            writer.writerow(
                (
                    int(row["id"]),
                    str(row["captured_at"]),
                    row["dominant_script"] if row["dominant_script"] is not None else "",
                    int(row["ocr_length"]),
                )
            )
            rows_written += 1

    if rows_written == 0:
        raise AppNotFoundError(app_name)

    log.info(
        "app_shots_csv.render.ok",
        app_name=app_name,
        rows=rows_written,
    )
    return buffer.getvalue()


__all__ = ["AppNotFoundError", "router"]
