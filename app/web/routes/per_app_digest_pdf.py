"""HTTP route for the per-app digest PDF.

``GET /apps/{app_name}/digest.pdf?days=7`` streams the PDF produced by
:func:`app.per_app_digest_pdf.build_per_app_digest_pdf` — a printable
sibling to the HTML page served from
:mod:`app.web.routes.per_app_digest`.

Status branches:

* ``200`` — PDF stream (``application/pdf``, attachment).
* ``404`` — ``app_name`` has never been captured.
* ``503`` — neither ``reportlab`` nor ``weasyprint`` is installed
  (JSON body ``{"detail": "pdf_backend_missing", ...}``).
"""

from __future__ import annotations

import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.logging_setup import get_logger
from app.per_app_digest_pdf import build_per_app_digest_pdf

log = get_logger("persona.per_app_digest_pdf")

router = APIRouter(tags=["per-app-digest"])

# Chunk size used to stream the in-memory PDF buffer to the client. Matches
# :mod:`app.web.routes.share_collection_pdf` for consistency.
_PDF_CHUNK_BYTES = 64 * 1024


def _safe_filename(app_name: str, days: int) -> str:
    """Return a filename safe for ``Content-Disposition``.

    Replaces anything outside ``[A-Za-z0-9._-]`` with ``_`` so the header
    stays ASCII-clean without surprising the user agent.
    """
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_" for ch in app_name
    )
    if not cleaned:
        cleaned = "app"
    return f"persona-{cleaned}-digest-{days}d.pdf"


@router.get("/apps/{app_name}/digest.pdf", response_model=None)
async def per_app_digest_pdf(
    app_name: str,
    days: int = Query(default=7, ge=1, le=365, description="Look-back window in days"),
) -> StreamingResponse | JSONResponse:
    """Stream the per-app digest PDF for ``app_name`` over the last ``days`` days."""
    target = app_name.strip()
    if not target:
        raise HTTPException(
            status_code=400,
            detail="app_name must be a non-empty string",
        )

    pdf_bytes = await build_per_app_digest_pdf(target, days=days)

    if pdf_bytes is None:
        # Disambiguate "no such app" from "no backend" — the per_app_digest_pdf
        # module emits its own structured log for each branch, but we still need
        # the route to pick the right HTTP status. Re-check backend availability
        # cheaply here.
        backend_available = False
        try:
            import reportlab  # noqa: F401, PLC0415
        except ImportError:
            try:
                import weasyprint  # noqa: F401, PLC0415
            except ImportError:
                backend_available = False
            else:
                backend_available = True
        else:
            backend_available = True

        if not backend_available:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "pdf_backend_missing",
                    "app_name": target,
                    "days": days,
                    "hint": (
                        "Install one of: 'uv pip install reportlab' "
                        "or 'uv pip install weasyprint'."
                    ),
                },
            )

        raise HTTPException(
            status_code=404,
            detail=f"App not found: {target}",
        )

    filename = _safe_filename(target, days)

    log.info(
        "per_app_digest_pdf.served",
        app_name=target,
        days=days,
        size_bytes=len(pdf_bytes),
        filename=filename,
    )

    buf = io.BytesIO(pdf_bytes)

    def _iter_buffer() -> object:
        while True:
            chunk = buf.read(_PDF_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk

    return StreamingResponse(
        _iter_buffer(),  # type: ignore[arg-type]
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


__all__ = ["router"]
