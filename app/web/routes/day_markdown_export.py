"""HTTP surfaces for the per-day Markdown export.

Two endpoints share this router:

* ``GET /export/day/{day_iso}.md`` — returns the raw markdown body as
  ``text/markdown; charset=utf-8`` with a ``Content-Disposition:
  attachment`` header so the browser downloads it as a real file.
* ``GET /day/{day_iso}/md`` — HTML preview page that extends
  :file:`base.html` and renders the same markdown through the
  ``markdown-it`` CDN script that :file:`base.html` already loads. A
  prominent "Download .md" button on the page links straight back to
  the attachment endpoint, so the user can preview before saving.

Both endpoints validate ``day_iso`` strictly (``YYYY-MM-DD``) and
return a 400 on malformed input. A 404 is returned when the day has no
exportable content (zero shots, zero notes, zero hourly cards) so the
caller can distinguish "empty day" from "wrong URL".

This module is import-side-effect-free; the task spec forbids touching
``app.web.main`` so the router is exported and a follow-up wires it in
with the standard ``app.include_router`` call.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.day_markdown import build_day_md
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.day_markdown_export")

router = APIRouter(tags=["day-markdown-export"])


def _validate_day_iso(day_iso: str) -> str:
    """Strictly parse ``YYYY-MM-DD``; return the canonical ISO string.

    Both endpoints share this guard so the failure mode is identical
    whether the user is downloading or previewing — a typo always gets
    a 400 with a one-line hint rather than silently falling back to
    "today".
    """
    try:
        parsed: date = datetime.strptime(day_iso, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="day_iso must be in YYYY-MM-DD form",
        ) from exc
    return parsed.isoformat()


@router.get("/export/day/{day_iso}.md")
async def export_day_markdown(day_iso: str) -> Response:
    """Return the comprehensive per-day Markdown export as a download.

    The response is ``text/markdown; charset=utf-8`` and carries a
    ``Content-Disposition: attachment; filename="persona-day-…
    .md"`` header so the browser saves it instead of trying to render
    the raw markdown inline.
    """
    canonical = _validate_day_iso(day_iso)
    body = await build_day_md(canonical)
    if not body:
        log.info("day_markdown.export.empty", day=canonical)
        raise HTTPException(
            status_code=404,
            detail=f"No exportable content for {canonical}",
        )
    filename = f"persona-day-{canonical}.md"
    log.info(
        "day_markdown.export.served",
        day=canonical,
        bytes=len(body.encode("utf-8")),
    )
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/day/{day_iso}/md", response_class=HTMLResponse)
async def preview_day_markdown(
    request: Request, day_iso: str
) -> HTMLResponse:
    """Render the markdown export inside a Persona-themed HTML page.

    The template hands the raw markdown body to ``markdown-it`` on the
    client (the CDN script is already loaded by :file:`base.html`), so
    we ship the markdown verbatim — no server-side HTML conversion,
    which keeps the route logic identical to the download endpoint.
    """
    canonical = _validate_day_iso(day_iso)
    body = await build_day_md(canonical)
    if not body:
        log.info("day_markdown.preview.empty", day=canonical)
        raise HTTPException(
            status_code=404,
            detail=f"No exportable content for {canonical}",
        )
    log.info(
        "day_markdown.preview.served",
        day=canonical,
        bytes=len(body.encode("utf-8")),
    )
    return templates.TemplateResponse(
        request,
        "day_markdown_preview.html",
        {
            "title": f"Day journal — {canonical}",
            "active_nav": "timeline",
            "day": canonical,
            "markdown_body": body,
            "download_url": f"/export/day/{canonical}.md",
        },
    )


__all__ = ["router"]
