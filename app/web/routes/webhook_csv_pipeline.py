"""Settings + send-now endpoints for the nightly CSV webhook pipeline (v1.62).

Four surfaces, all rooted at ``/settings/webhook-csv`` and
``/api/webhook-csv``:

* ``GET  /settings/webhook-csv``               — HTML form (current
                                                  destinations + new-row form).
* ``POST /settings/webhook-csv/new``           — upsert one row from
                                                  the form, PRG back.
* ``POST /settings/webhook-csv/{id}/delete``   — hard-delete one row,
                                                  PRG back to the form.
* ``POST /api/webhook-csv/{id}/send-now``      — fire one immediate
                                                  POST for the given
                                                  destination via the
                                                  same pipeline helper
                                                  the worker uses.

The page reads the existing ``webhook_csv_pipeline_enabled`` kv row
only to display the worker's status badge — it never writes the
toggle from here (the worker control lives on the global settings
hub so it can be toggled even when zero destinations exist).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.web.templates_engine import templates
from app.webhook_csv_pipeline import (
    delete_destination,
    list_destinations,
    send_destination,
    upsert_destination,
)

router = APIRouter(tags=["webhook-csv-pipeline"])
log = get_logger("persona.web.webhook_csv_pipeline")

# kv row name shared with :mod:`app.workers.webhook_csv_worker`.
_KV_ENABLED: str = "webhook_csv_pipeline_enabled"

# Human labels for the four ``csv_kind`` values — Russian to match
# the rest of the settings UI. The dict order matches the order they
# render in the form's ``<select>`` so the typical "screenshots"
# default sits first.
_KIND_LABELS_RU: dict[str, str] = {
    "screenshots": "скриншоты",
    "notes": "заметки",
    "hourly_cards": "часовые карточки",
    "audio_segments": "аудио-сегменты",
}


async def _worker_enabled() -> bool:
    """Read the worker's top-level toggle for the UI status badge."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


@router.get("/settings/webhook-csv", response_class=HTMLResponse)
async def webhook_csv_page(request: Request) -> HTMLResponse:
    """Render the destinations list + new-row form."""
    destinations = await list_destinations()
    worker_on = await _worker_enabled()
    return templates.TemplateResponse(
        request,
        "webhook_csv_pipeline.html",
        {
            "title": "Webhook CSV pipeline",
            "active_nav": "settings",
            "destinations": destinations,
            "kind_labels": _KIND_LABELS_RU,
            "worker_enabled": worker_on,
        },
    )


@router.post("/settings/webhook-csv/new")
async def webhook_csv_create(
    name: str = Form(...),
    webhook_url: str = Form(...),
    csv_kind: str = Form(...),
    days_window: int = Form(1),
    hour_local: int = Form(5),
    enabled: str = Form("on"),
    headers_json: str = Form(""),
) -> RedirectResponse:
    """Upsert one destination from the form, PRG back to the list.

    The ``enabled`` field arrives as ``"on"`` (checked) or is absent
    (unchecked) — FastAPI gives us ``""`` for the absent case, so any
    truthy non-empty string is mapped to ``True``. Mirrors the
    convention used by other settings forms in the project.
    """
    try:
        dest_id = await upsert_destination(
            name=name,
            webhook_url=webhook_url,
            csv_kind=csv_kind,
            days_window=int(days_window),
            hour_local=int(hour_local),
            enabled=bool(enabled),
            headers_json=headers_json or None,
        )
    except ValueError as exc:
        log.warning("webhook_csv.upsert.bad_input", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Deliberately omit ``webhook_url`` from the log line.
    log.info("webhook_csv.upsert.ok", dest_id=dest_id, name=name)
    return RedirectResponse(url="/settings/webhook-csv", status_code=303)


@router.post("/settings/webhook-csv/{dest_id}/delete")
async def webhook_csv_delete(dest_id: int) -> RedirectResponse:
    """Hard-delete one row, PRG back to the form."""
    await delete_destination(int(dest_id))
    return RedirectResponse(url="/settings/webhook-csv", status_code=303)


@router.post("/api/webhook-csv/{dest_id}/send-now")
async def webhook_csv_send_now(dest_id: int) -> JSONResponse:
    """Fire one immediate POST for the given destination.

    Returns the raw status dict from :func:`send_destination` so the
    UI can render the four outcomes (sent / http_error /
    transport_error / disabled / missing) without translating them
    twice. DOES advance ``last_sent_at`` — the operator's intent here
    is "make this dump real, like the worker would have done at the
    scheduled hour" — so the next scheduled tick will see a fresh
    floor and skip the row.
    """
    result = await send_destination(int(dest_id))
    log.info(
        "webhook_csv.send_now.result",
        dest_id=int(dest_id),
        status=result.get("status"),
        status_code=result.get("status_code"),
        body_bytes_sent=result.get("body_bytes_sent"),
    )
    if result.get("status") == "missing":
        raise HTTPException(status_code=404, detail="destination not found")
    return JSONResponse(result)


__all__ = ["router"]
