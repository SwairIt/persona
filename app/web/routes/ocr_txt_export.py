"""HTTP route for the per-day OCR text dump.

``GET /export/ocr.txt?day=YYYY-MM-DD`` streams a ``text/plain``
document built by :func:`app.ocr_txt_export.export_day_ocr_txt`.

The intended workflow is::

    curl -s "http://localhost:8000/export/ocr.txt?day=2026-06-01" \\
        | grep -i "kubernetes"

so the response is served as a download (``Content-Disposition:
attachment``) with a date-stamped filename, plain UTF-8, and a
``Cache-Control: no-store`` to keep stale grep targets out of browser
caches.  Defaults ``day`` to *today* (local date) so a parameter-less
``curl`` still produces something useful.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.ocr_txt_export import export_day_ocr_txt

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.ocr_txt")

router = APIRouter(prefix="/export", tags=["ocr-txt-export"])


def _today_local_iso() -> str:
    """Return today's local date as ``YYYY-MM-DD``.

    Lifted into a tiny helper so the default-day branch is trivially
    overridable from tests (monkeypatch the symbol on this module).
    """
    return datetime.now().astimezone().date().isoformat()


def _validate_day(day: str | None) -> str:
    """Validate ``?day=`` and return a canonical ``YYYY-MM-DD`` string.

    None / empty → today.  Anything else is parsed strictly with
    ``%Y-%m-%d`` so we reject ``2026/06/01``, ``20260601`` and other
    near-misses at the route boundary instead of letting them surface
    as a 500 from the SQL layer.
    """
    if not day:
        return _today_local_iso()
    try:
        parsed: date = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"invalid day {day!r} (expected YYYY-MM-DD)"
        raise HTTPException(status_code=400, detail=msg) from exc
    return parsed.isoformat()


@router.get("/ocr.txt", response_model=None)
async def export_ocr_txt(
    day: str | None = Query(default=None, description="Local day, YYYY-MM-DD; default today."),
) -> StreamingResponse:
    """Stream the per-day OCR text dump."""
    day_iso = _validate_day(day)

    try:
        body = await export_day_ocr_txt(day_iso)
    except ValueError as exc:
        # Defensive — ``_validate_day`` already filtered bad formats,
        # but the export function may raise its own ValueError if a
        # future caller bypasses the validator.
        log.warning("ocr_txt.route.bad_day", day=day_iso, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        log.exception("ocr_txt.route.failed", day=day_iso)
        raise HTTPException(status_code=500, detail="OCR text export failed") from None

    payload = body.encode("utf-8")
    filename = f"persona-ocr-{day_iso}.txt"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("ocr_txt.route.ok", day=day_iso, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
