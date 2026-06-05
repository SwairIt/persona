"""Settings + test endpoints for the per-tag weekly email digest (v1.61).

Five surfaces, all rooted at ``/settings/tag-email-digest`` and
``/api/tag-email-digest``:

* ``GET  /settings/tag-email-digest``             — HTML form (current
                                                     subs + new-sub form).
* ``POST /settings/tag-email-digest/new``         — upsert one row from
                                                     the form, PRG back.
* ``POST /settings/tag-email-digest/{id}/delete`` — hard-delete one row,
                                                     PRG back to the form.
* ``POST /api/tag-email-digest/send-now/{id}``    — fire one immediate
                                                     send for the given
                                                     subscription via the
                                                     same SMTP helper the
                                                     worker uses.

The page reads the existing ``tag_email_digest_enabled`` kv row only to
display the worker's status badge — it never writes the toggle from
here (the worker control lives on the global settings hub so it can be
toggled even when zero subs exist).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.smtp_delivery import send_digest_email
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.tag_email_digest import (
    build_tag_digest_body,
    delete_subscription,
    list_subscriptions,
    upsert_subscription,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["tag-email-digest"])
log = get_logger("persona.web.tag_email_digest")

# kv row name shared with :mod:`app.workers.tag_email_digest_worker`.
_KV_ENABLED: str = "tag_email_digest_enabled"

# Human labels for ``day_of_week`` 0..6 — Russian to match the rest of
# the settings UI. Order is Mon..Sun so the index lines up with
# ``date.weekday()`` semantics.
_DAY_LABELS_RU: tuple[str, ...] = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _current_week_start_iso() -> str:
    """Return the Monday of the current local ISO week, ``YYYY-MM-DD``."""
    today = datetime.now().astimezone().date()
    return (today - timedelta(days=today.weekday())).isoformat()


async def _worker_enabled() -> bool:
    """Read the worker's top-level toggle for the UI status badge."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


@router.get("/settings/tag-email-digest", response_class=HTMLResponse)
async def tag_email_digest_page(request: Request) -> HTMLResponse:
    """Render the subscriptions list + new-sub form."""
    subs = await list_subscriptions()
    worker_on = await _worker_enabled()
    return templates.TemplateResponse(
        request,
        "tag_email_digest.html",
        {
            "title": "Email подписки по тегу",
            "active_nav": "settings",
            "subs": subs,
            "day_labels": _DAY_LABELS_RU,
            "worker_enabled": worker_on,
            "week_start_iso": _current_week_start_iso(),
        },
    )


@router.post("/settings/tag-email-digest/new")
async def tag_email_digest_create(
    tag: str = Form(...),
    email: str = Form(...),
    day_of_week: int = Form(6),
    hour_local: int = Form(19),
) -> RedirectResponse:
    """Upsert one subscription from the form, PRG back to the list."""
    try:
        sub_id = await upsert_subscription(
            tag=tag,
            email=email,
            day_of_week=int(day_of_week),
            hour_local=int(hour_local),
        )
    except ValueError as exc:
        log.warning("tag_email_digest.upsert.bad_input", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("tag_email_digest.upsert.ok", sub_id=sub_id)
    return RedirectResponse(
        url="/settings/tag-email-digest", status_code=303
    )


@router.post("/settings/tag-email-digest/{sub_id}/delete")
async def tag_email_digest_delete(sub_id: int) -> RedirectResponse:
    """Hard-delete one row, PRG back to the form."""
    await delete_subscription(int(sub_id))
    return RedirectResponse(
        url="/settings/tag-email-digest", status_code=303
    )


@router.post("/api/tag-email-digest/send-now/{sub_id}")
async def tag_email_digest_send_now(sub_id: int) -> JSONResponse:
    """Fire one immediate send for the given subscription.

    Returns the raw status dict from :func:`send_digest_email` so the
    UI can render ``sent`` / ``disabled`` / ``misconfigured`` /
    ``missing_dep`` / ``error`` without translating it twice. Does NOT
    advance the worker's ``last_sent_at`` — this is a test fire, not
    the scheduled run.
    """
    subs = await list_subscriptions()
    target = next((s for s in subs if s["id"] == int(sub_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="subscription not found")

    tag = str(target["tag"])
    week_start_iso = _current_week_start_iso()
    body_html = await build_tag_digest_body(tag, week_start_iso)
    subject = f"Persona — #{tag} — week of {week_start_iso} (test)"
    body_text = (
        f"Persona per-tag digest for #{tag} "
        f"(week of {week_start_iso}). Test send from the settings UI.\n"
    )
    result = await send_digest_email(subject, body_text, body_html)
    log.info(
        "tag_email_digest.send_now.result",
        sub_id=int(sub_id),
        tag=tag,
        status=result.get("status"),
    )
    return JSONResponse(result)


__all__ = ["router"]
