"""HTTP route for the share-collection PDF export.

``GET /collection/{slug}/export.pdf`` streams the PDF produced by
:func:`app.share_collection_pdf.build_collection_pdf`. ``slug`` is the
signed token minted by :mod:`app.web.routes.share_collection` — same
identifier the public viewer at ``/share/collection/{token}`` accepts.

When ``reportlab`` is not installed we serve an HTML banner explaining
the optional dependency instead of a hard 500, matching the contract
the rest of the PDF surfaces (:mod:`app.web.routes.pdf_export`,
:mod:`app.web.routes.weekly_pdf`) already follow.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from app.logging_setup import get_logger
from app.share_collection_pdf import build_collection_pdf

log = get_logger("persona.share_collection_pdf")

router = APIRouter(tags=["share"])

# Streaming chunk size — mirrors :mod:`app.web.routes.pdf_export` so a
# big collection doesn't blow up RSS while it's being shipped to the
# client.
_PDF_CHUNK_BYTES = 64 * 1024


def _missing_dep_html(slug: str) -> HTMLResponse:
    """Render an explanatory 503 when ``reportlab`` is not installed.

    Kept in lockstep with :func:`app.web.routes.pdf_export._missing_dep_html`
    so the user sees the same look-and-feel across Persona's PDF surfaces.
    """
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Persona — share-collection PDF unavailable</title>
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
    <h1>Share-collection PDF unavailable</h1>
    <p>The optional <code>reportlab</code> dependency is not installed,
       so Persona cannot render a PDF bundle for this share collection.</p>
    <p>Install it with:</p>
    <p><code>uv pip install reportlab</code></p>
    <p>Then reload <code>/collection/{slug}/export.pdf</code>.</p>
  </div>
</body>
</html>
"""
    return HTMLResponse(content=body, status_code=503)


@router.get("/collection/{slug}/export.pdf", response_model=None)
async def export_collection_pdf(
    slug: str,
) -> StreamingResponse | HTMLResponse:
    """Stream the share-collection PDF for ``slug``.

    Status branches mirror :class:`app.share_collection_pdf.CollectionPdfResult`:

    * ``missing_dep`` → 503 HTML banner (reportlab not installed).
    * ``not_found`` → 404.
    * ``expired`` → 403 (mirrors the public viewer's response).
    * ``corrupt`` → 500 (hand-edited row; surfaces the bug instead of hiding it).
    * ``empty`` → 404 — every referenced shot was hard-deleted.
    * ``ok`` → streamed ``application/pdf`` attachment.
    """
    if not slug or "/" in slug:
        # Defensive: slugs come straight from the URL. Anything containing
        # a path separator can't be a legitimate signed token and would
        # otherwise pollute the temp filename below.
        raise HTTPException(status_code=400, detail="Invalid slug")

    tmp_dir = Path(tempfile.gettempdir()) / "persona-share-collection-pdf"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Token strings are URL-safe (base64-url) so they make a fine
    # filename without further sanitising; the leading slash check
    # above is the only stricture the OS layer needs.
    out_path = tmp_dir / f"persona-share-collection-{slug}.pdf"

    result = await build_collection_pdf(slug, out_path)

    if result["status"] == "missing_dep":
        return _missing_dep_html(slug)
    if result["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Collection not found")
    if result["status"] == "expired":
        raise HTTPException(status_code=403, detail="Collection expired")
    if result["status"] == "corrupt":
        log.error("share_collection_pdf.corrupt_row", slug=slug)
        raise HTTPException(status_code=500, detail="Collection data corrupt")
    if result["status"] == "empty":
        raise HTTPException(
            status_code=404,
            detail="No screenshots remain in this collection",
        )
    if result["status"] != "ok" or result["path"] is None:
        log.error(
            "share_collection_pdf.unexpected_status",
            slug=slug,
            status=result["status"],
        )
        raise HTTPException(status_code=500, detail="PDF export failed")

    pdf_path = Path(result["path"])

    def _iter_file() -> object:
        with pdf_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PDF_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    filename = f"persona-share-collection-{slug}.pdf"
    return StreamingResponse(
        _iter_file(),  # type: ignore[arg-type]
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(result["size_bytes"]),
        },
    )


__all__ = ["router"]
