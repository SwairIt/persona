"""SMTP delivery settings — read/write UI and one-off test send (v0.31).

The eight ``smtp_*`` rows in ``kv_settings`` are managed entirely from
this page. The password value is *masked* on render (the form ships an
``***`` placeholder when a real password is on file, and an empty form
field never overwrites the stored secret) so leaving the field blank
during a save is a no-op rather than a wipe.

POST ``/settings/smtp/test`` reuses :func:`send_digest_email` with a
synthetic subject/body so the user gets the exact same code path as a
real digest, including all of the misconfigured / missing-dep / error
status branches.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.smtp_delivery import send_digest_email
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.smtp")

# Sentinel rendered into the password field whenever a real password is
# already on file. POSTs that arrive with this exact value are treated
# as "no change" so navigating away and back never re-saves the placeholder.
_PASSWORD_MASK = "***"  # noqa: S105 — UI placeholder, not a credential

_KEYS: tuple[str, ...] = (
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_pass",
    "smtp_to",
    "smtp_from",
    "smtp_tls",
    "smtp_enabled",
)


async def _load_all() -> dict[str, str]:
    """Read the eight smtp_* kv rows, defaulting absent keys to ``''``."""
    async with get_connection() as conn:
        values: dict[str, str] = {}
        for key in _KEYS:
            raw = await get_kv(conn, key)
            values[key] = "" if raw is None else raw
    return values


def _mask_for_render(values: dict[str, str]) -> dict[str, str]:
    """Replace the real password with a fixed mask before handing to Jinja."""
    rendered = dict(values)
    rendered["smtp_pass"] = _PASSWORD_MASK if values.get("smtp_pass") else ""
    return rendered


@router.get("/settings/smtp", response_class=HTMLResponse)
async def smtp_settings_page(request: Request) -> HTMLResponse:
    """Render the SMTP configuration form with the password masked."""
    values = await _load_all()
    return templates.TemplateResponse(
        request,
        "smtp_settings.html",
        {
            "title": "SMTP delivery",
            "active_nav": "settings",
            "values": _mask_for_render(values),
            "has_password": bool(values.get("smtp_pass")),
        },
    )


@router.post("/settings/smtp")
async def smtp_settings_save(
    request: Request,
    smtp_host: str = Form(default=""),
    smtp_port: str = Form(default="587"),
    smtp_user: str = Form(default=""),
    smtp_pass: str = Form(default=""),
    smtp_to: str = Form(default=""),
    smtp_from: str = Form(default=""),
    smtp_tls: str = Form(default="false"),
    smtp_enabled: str = Form(default="false"),
) -> RedirectResponse:
    """Persist all eight rows. Leaves the password untouched if blank/masked.

    Checkboxes that are absent from the form payload come through as
    their ``default=`` ("false") thanks to FastAPI's Form handling, so
    unchecking the box reliably persists as ``smtp_enabled='false'``.
    """
    incoming: dict[str, str] = {
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port.strip() or "587",
        "smtp_user": smtp_user.strip(),
        "smtp_to": smtp_to.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_tls": "true" if smtp_tls.strip().lower() == "true" else "false",
        "smtp_enabled": "true" if smtp_enabled.strip().lower() == "true" else "false",
    }

    async with get_connection() as conn:
        for key, value in incoming.items():
            await set_kv(conn, key, value)

        # Password: only overwrite if the user typed something new.
        # The empty string and the mask both mean "keep what's on file".
        if smtp_pass and smtp_pass != _PASSWORD_MASK:
            await set_kv(conn, "smtp_pass", smtp_pass)

    # Троттлинг спрашивает, уходит ли почта вообще: штраф неподтверждённым
    # аккаунтам включается только там, где письмо реально можно доставить.
    # Ответ кэшируется на 5 минут — после явного сохранения настроек честнее
    # пересчитать сразу.
    from app.auth.verification import reset_mail_cache  # noqa: PLC0415

    reset_mail_cache()

    log.info(
        "smtp.settings.saved",
        enabled=incoming["smtp_enabled"],
        host=incoming["smtp_host"] or "(unset)",
        port=incoming["smtp_port"],
        starttls=incoming["smtp_tls"],
        password_updated=bool(smtp_pass and smtp_pass != _PASSWORD_MASK),
    )
    return RedirectResponse(url="/settings/smtp", status_code=303)


@router.post("/settings/smtp/test")
async def smtp_settings_test(request: Request) -> JSONResponse:
    """Send a one-off test message via the configured relay.

    Returns the raw status dict from :func:`send_digest_email` so the
    UI can render either ``sent`` / ``disabled`` / ``misconfigured``
    / ``missing_dep`` / ``error`` without translating it twice. HTTP
    status is always 200 — the *payload* tells the front-end what to do.
    """
    subject = "Persona SMTP test"
    body = (
        "This is a test message from Persona.\n\n"
        "If you can read it your SMTP relay is configured correctly "
        "and Persona will use it for daily and weekly LLM digests."
    )
    body_html = (
        "<p>This is a test message from <strong>Persona</strong>.</p>"
        "<p>If you can read it your SMTP relay is configured correctly "
        "and Persona will use it for daily and weekly LLM digests.</p>"
    )
    result = await send_digest_email(subject, body, body_html)
    log.info("smtp.test.result", status=result.get("status"))
    return JSONResponse(result)
