"""HTTP route for the per-day PDF export.

``GET /export/pdf?day=YYYY-MM-DD`` streams the PDF produced by
:func:`app.pdf_export.export_day_pdf`. When ``reportlab`` is not installed
we serve an HTML banner explaining the optional dependency instead of a
hard 500 — the rest of Persona keeps working without it.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app.logging_setup import get_logger
from app.pdf_export import export_day_pdf

log = get_logger("persona.pdf")

router = APIRouter(prefix="/export", tags=["pdf-export"])

_PDF_CHUNK_BYTES = 64 * 1024


def _today_iso() -> str:
    return date.today().isoformat()


def _validate_day(day: str) -> str:
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid day (expected YYYY-MM-DD)"
        ) from exc
    return day


def _missing_dep_html(day: str) -> HTMLResponse:
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Persona — PDF export unavailable</title>
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
    <h1>PDF export unavailable</h1>
    <p>The optional <code>reportlab</code> dependency is not installed,
       so Persona cannot render a PDF for <b>{day}</b>.</p>
    <p>Install it with:</p>
    <p><code>uv pip install reportlab</code></p>
    <p>Then reload this page.</p>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=body, status_code=503)


@router.get("/pdf", response_model=None)
async def export_day_pdf_route(
    day: str = Query(default_factory=_today_iso),
) -> StreamingResponse | HTMLResponse:
    """Stream the per-day PDF — or an HTML banner when reportlab is absent."""
    day = _validate_day(day)

    tmp_dir = Path(tempfile.gettempdir()) / "persona-pdf"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / f"persona-day-{day}.pdf"

    result = await export_day_pdf(day, out_path)

    if result["status"] == "missing_dep":
        return _missing_dep_html(day)
    if result["status"] == "bad_date":
        # _validate_day should have caught this — defence in depth.
        raise HTTPException(status_code=400, detail="Invalid day")
    if result["status"] == "empty":
        raise HTTPException(
            status_code=404, detail=f"No screenshots for {day}"
        )
    if result["status"] != "ok" or result["path"] is None:
        log.error("pdf.unexpected_status", day=day, status=result["status"])
        raise HTTPException(status_code=500, detail="PDF export failed")

    pdf_path = Path(result["path"])

    def _iter_file() -> object:
        with pdf_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PDF_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    filename = f"persona-day-{day}.pdf"
    return StreamingResponse(
        _iter_file(),  # type: ignore[arg-type]
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(result["size_bytes"]),
        },
    )


__all__ = ["router"]
