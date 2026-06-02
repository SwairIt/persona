"""HTTP route for the weekly PDF export.

``GET /export/weekly-pdf?week=YYYY-MM-DD`` streams the PDF produced by
:func:`app.weekly_pdf.export_week_pdf`. When ``reportlab`` is not installed
we serve an HTML banner explaining the optional dependency instead of a
hard 500 — the rest of Persona keeps working without it.

The ``week`` parameter accepts any ISO date inside the desired week; the
underlying helper normalises it to that week's Monday. When the parameter
is omitted we default to the most recent Monday (today if today is Monday).
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app.logging_setup import get_logger
from app.weekly_pdf import export_week_pdf

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.weekly_pdf")

router = APIRouter(prefix="/export", tags=["weekly-pdf"])

_PDF_CHUNK_BYTES = 64 * 1024


def _last_monday_iso() -> str:
    """Return the most recent Monday as ``YYYY-MM-DD`` (today if today is Mon)."""
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def _validate_week(week: str) -> str:
    """Reject non ``YYYY-MM-DD`` input early with a 400 so the helper isn't called."""
    try:
        datetime.strptime(week, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid week (expected YYYY-MM-DD)"
        ) from exc
    return week


def _missing_dep_html(week: str) -> HTMLResponse:
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Persona — weekly PDF unavailable</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #f9fafb; color: #111827;
          margin: 0; padding: 3rem; }}
  .card {{ max-width: 560px; margin: 0 auto; background: white; border-radius: 12px;
           padding: 2rem; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
  code {{ background: #f3f4f6; padding: 2px 6px; border-radius: 4px; }}
  h1 {{ margin-top: 0; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Weekly PDF unavailable</h1>
    <p>The optional <code>reportlab</code> dependency is not installed,
       so Persona cannot render the weekly PDF for the week of <b>{week}</b>.</p>
    <p>Install it with:</p>
    <p><code>uv pip install reportlab</code></p>
    <p>Then reload this page.</p>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=body, status_code=503)


@router.get("/weekly-pdf", response_model=None)
async def export_weekly_pdf_route(
    week: str = Query(default_factory=_last_monday_iso),
) -> StreamingResponse | HTMLResponse:
    """Stream the weekly PDF — or an HTML banner when reportlab is absent."""
    week = _validate_week(week)

    tmp_dir = Path(tempfile.gettempdir()) / "persona-weekly-pdf"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / f"persona-week-{week}.pdf"

    result = await export_week_pdf(week, out_path)

    if result["status"] == "missing_dep":
        return _missing_dep_html(week)
    if result["status"] == "bad_date":
        # _validate_week should have caught this — defence in depth.
        raise HTTPException(status_code=400, detail="Invalid week")
    if result["status"] != "ok" or result["path"] is None:
        log.error("weekly_pdf.unexpected_status", week=week, status=result["status"])
        raise HTTPException(status_code=500, detail="Weekly PDF export failed")

    pdf_path = Path(result["path"])
    normalised_week = result["week_start"] or week

    def _iter_file() -> Iterator[bytes]:
        with pdf_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PDF_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    filename = f"persona-week-{normalised_week}.pdf"
    return StreamingResponse(
        _iter_file(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(result["size_bytes"]),
        },
    )


__all__ = ["router"]
