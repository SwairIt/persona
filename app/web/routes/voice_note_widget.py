"""HTMX-friendly HTML fragment with the record button + uploader.

Single route: ``GET /widget/voice-note``. Returns the rendered
:file:`_voice_note_widget.html` partial (no ``base.html`` chrome) so any
page can drop the widget in via ::

    <div hx-get="/widget/voice-note" hx-trigger="load"></div>

The fragment is self-contained — its own ``<button>``, status ``<div>``,
and inline ``<script>`` that owns the ``MediaRecorder`` lifecycle and
the POST to :mod:`app.web.routes.voice_note`. There is no server-side
state in the widget itself; multiple instances can coexist on one page
because the script scopes every DOM lookup to the wrapper element via a
generated ``data-widget-id`` attribute.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import voice_note_widget as voice_note_widget_routes
    app.include_router(voice_note_widget_routes.router)
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["voice-note-widget"])

log = get_logger("persona.voice_note.widget")


@router.get("/widget/voice-note", response_class=HTMLResponse)
async def voice_note_widget(request: Request) -> HTMLResponse:
    """Render the record-button fragment for embedding via HTMX.

    A short random ``widget_id`` is injected into the template so each
    HTMX swap produces a uniquely-scoped DOM subtree — two widgets on
    the same page (e.g. one on the dashboard, one on the notes inbox)
    do not stomp on each other's status div or recorder state.

    The fragment is intentionally returned even when no Whisper backend
    is installed: the JS will still let the operator record, the POST
    will come back ``503``, and the script will surface the
    server-supplied error string. Failing-open at render time keeps the
    UI consistent.
    """
    # 8 hex chars — collision probability is irrelevant for a per-render
    # value, but we don't want predictable ids that a malicious other-tab
    # script could target with ``querySelector``.
    widget_id = secrets.token_hex(4)
    log.info("voice_note.widget.render", widget_id=widget_id)
    return templates.TemplateResponse(
        request,
        "_voice_note_widget.html",
        {"widget_id": widget_id},
    )


__all__ = ["router"]
