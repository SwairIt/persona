"""HTTP surface for the single-shot manual OCR re-run.

One POST route:

* ``POST /api/screenshot/{shot_id}/ocr-rerun`` — re-process this shot
  with the current Tesseract settings and return the
  :class:`app.ocr_rerun.RerunResult` payload as JSON.

  Returns ``200`` for every non-fatal outcome (``ok`` /
  ``missing_image`` / ``ocr_failed``) so HTMX's default afterRequest
  swap path treats the response as a success and the
  ``location.reload()`` hook fires; the operator sees the new OCR text
  immediately. ``404`` is reserved for ``shot_not_found`` because that
  is the standard FastAPI-shaped contract for a missing resource.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — per task spec, ``main.py`` is off-limits. Wire
it up with::

    from app.web.routes import ocr_rerun as ocr_rerun_routes
    app.include_router(ocr_rerun_routes.router)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.ocr_rerun import RerunResult, rerun_ocr_for_shot

log = get_logger("persona.ocr_rerun.route")

router = APIRouter(tags=["ocr-rerun"])


@router.post("/api/screenshot/{shot_id}/ocr-rerun")
async def ocr_rerun(shot_id: int) -> RerunResult:
    """Re-run OCR for one screenshot. See :func:`rerun_ocr_for_shot`."""
    result = await rerun_ocr_for_shot(shot_id)
    if result["status"] == "shot_not_found":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return result


@router.get("/shot/{shot_id}/ocr-rerun-button", response_class=HTMLResponse)
async def ocr_rerun_button_fragment(shot_id: int) -> HTMLResponse:
    """Standalone fallback page with a single Re-run OCR button.

    Lets the operator trigger the re-run even if the screenshot detail
    template has not picked up the inline button (e.g. on a deployment
    that has not been redeployed since v1.23). The page is intentionally
    minimal — no base layout, no nav — so it can be linked directly
    from an admin bookmarklet.
    """
    log.info("ocr_rerun.fragment_rendered", shot_id=shot_id)
    html = (
        "<!doctype html>"
        "<html><head><meta charset='utf-8'>"
        f"<title>Re-run OCR for shot #{shot_id}</title>"
        "<script src='https://unpkg.com/htmx.org@1.9.10'></script>"
        "</head><body style='font-family:sans-serif;padding:2rem;background:#0b0b0d;color:#eee;'>"
        f"<h1>Re-run OCR — shot #{shot_id}</h1>"
        "<p>Click the button to re-process this screenshot with the current Tesseract "
        "settings. The page will reload on success so you can verify the new text on "
        f"<a href='/screenshot/{shot_id}' style='color:#7dd3fc;'>the detail view</a>.</p>"
        "<button type='button' "
        f"hx-post='/api/screenshot/{shot_id}/ocr-rerun' "
        "hx-swap='none' "
        f"hx-on::after-request=\"window.location.href='/screenshot/{shot_id}'\" "
        "style='padding:0.6rem 1.2rem;background:#7c3aed;color:white;border:none;"
        "border-radius:0.4rem;font-size:1rem;cursor:pointer;'>"
        "Re-run OCR"
        "</button>"
        "</body></html>"
    )
    return HTMLResponse(content=html)
