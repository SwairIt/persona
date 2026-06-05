"""HTTP surface for the per-day PDF export.

Single endpoint shipped here:

* ``GET /export/day/{day_iso}.pdf`` — returns the multi-page PDF render
  of the same content as ``/export/day/{day_iso}.md``. The response is
  ``application/pdf`` with a ``Content-Disposition: attachment;
  filename="persona-day-…pdf"`` header so the browser saves it instead
  of trying to render inline (some browsers will preview, that is fine
  — the header just guarantees a sensible default file name).

A companion ``/day/{day_iso}/pdf-preview`` HTML page is also exported
so users land on a small page with both download buttons when they
hit the ``/pdf-preview`` URL directly; the existing markdown preview
page (``/day/{day_iso}/md``) is surgically edited in the same patch
to gain a "Download PDF" button.

This module is import-side-effect-free; the task spec forbids touching
:mod:`app.web.main`, so the router is included from
``app.web.routes.day_markdown_export`` (which is already wired into
the FastAPI app) via ``APIRouter.include_router``.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.day_pdf import build_day_pdf
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.day_pdf_export")

router = APIRouter(tags=["day-pdf-export"])


def _validate_day_iso(day_iso: str) -> str:
    """Strictly parse ``YYYY-MM-DD``; return the canonical ISO string.

    Mirrors the guard from :mod:`app.web.routes.day_markdown_export` so
    the failure mode is identical across the two sibling endpoints —
    a typo always returns a 400 with a one-line hint rather than
    silently rendering "today".
    """
    try:
        parsed: date = datetime.strptime(day_iso, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="day_iso must be in YYYY-MM-DD form",
        ) from exc
    return parsed.isoformat()


@router.get("/export/day/{day_iso}.pdf")
async def export_day_pdf(day_iso: str) -> Response:
    """Return the per-day PDF export as a download.

    The response is ``application/pdf`` and carries a
    ``Content-Disposition: attachment; filename="persona-day-….pdf"``
    header so the browser saves it instead of trying to render the raw
    PDF inline (browsers that *do* render inline still get the
    sensible file name once the user clicks "Save as…").
    """
    canonical = _validate_day_iso(day_iso)
    try:
        payload = await build_day_pdf(canonical)
    except RuntimeError as exc:
        # Pillow missing — degrade to 503 so monitoring can flag the
        # install problem rather than the request being charged as a
        # client-side mistake.
        log.error("day_pdf.export.unavailable", day=canonical, error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="PDF export unavailable — Pillow is not installed.",
        ) from exc
    if not payload:
        log.info("day_pdf.export.empty", day=canonical)
        raise HTTPException(
            status_code=404,
            detail=f"No exportable content for {canonical}",
        )
    filename = f"persona-day-{canonical}.pdf"
    log.info(
        "day_pdf.export.served",
        day=canonical,
        bytes=len(payload),
    )
    return Response(
        content=payload,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/day/{day_iso}/pdf-preview", response_class=HTMLResponse)
async def preview_day_pdf(
    request: Request, day_iso: str
) -> HTMLResponse:
    """Render a tiny landing page with download buttons for both formats.

    The page does NOT embed the PDF — that would require a viewer
    component and a second HTTP round-trip. Instead it shows the day
    heading plus prominent "Download PDF" and "Download MD" buttons,
    so the user can pick the format and the browser does its native
    download dance. The page is also used as the fallback target when
    the existing markdown preview template cannot be patched safely.
    """
    canonical = _validate_day_iso(day_iso)
    log.info("day_pdf.preview.served", day=canonical)
    return templates.TemplateResponse(
        request,
        "day_pdf_preview.html",
        {
            "title": f"Day journal export - {canonical}",
            "active_nav": "timeline",
            "day": canonical,
            "pdf_url": f"/export/day/{canonical}.pdf",
            "md_url": f"/export/day/{canonical}.md",
            "md_preview_url": f"/day/{canonical}/md",
        },
    )


__all__ = ["router"]
