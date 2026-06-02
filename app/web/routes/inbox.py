"""HTML + form routes for the markdown notes inbox.

The background worker (:mod:`app.workers.inbox_worker`) drains the
folder every 30s on its own. These routes simply surface the current
state to the user and offer a manual *import now* button so the user
doesn't have to wait for the next tick after dropping a file in.

Routes:
    * ``GET  /inbox``             — status page (pending count + recent
                                    processed / failed entries).
    * ``POST /inbox/import-now``  — run one cycle synchronously and
                                    303-redirect back to ``GET /inbox``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates
from app.workers.inbox_worker import (
    count_pending,
    list_failed,
    list_processed,
    run_inbox_cycle,
)

log = get_logger("persona.inbox")

router = APIRouter(tags=["inbox"])


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request) -> HTMLResponse:
    """Render the inbox status page."""
    settings = get_settings()
    inbox_dir = settings.inbox_path

    pending = count_pending(inbox_dir)
    processed = list_processed(inbox_dir, limit=20)
    failed = list_failed(inbox_dir, limit=20)

    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "title": "Inbox",
            "active_nav": "journal",
            "inbox_enabled": settings.inbox_enabled,
            "inbox_dir": str(inbox_dir),
            "pending": pending,
            "processed": processed,
            "failed": failed,
        },
    )


@router.post("/inbox/import-now")
async def inbox_import_now() -> RedirectResponse:
    """Drain the inbox once synchronously and redirect back to the page."""
    settings = get_settings()
    if not settings.inbox_enabled:
        log.info("inbox.import_now.disabled")
        return RedirectResponse(url="/inbox", status_code=303)

    report = await run_inbox_cycle(settings.inbox_path)
    log.info(
        "inbox.import_now",
        scanned=report.scanned,
        imported=report.imported,
        failed=report.failed,
    )
    return RedirectResponse(url="/inbox", status_code=303)
