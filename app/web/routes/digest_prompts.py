"""Weekly + monthly digest prompt template editor (v0.56, v0.75).

Lets the user override the hard-coded ``_SYSTEM`` constants in
:mod:`app.llm.weekly_summariser` and :mod:`app.llm.monthly_summariser`
with their own Python ``str.format`` templates. Each template is
persisted in ``kv_settings`` under its own key
(``weekly_digest_prompt_template`` / ``monthly_digest_prompt_template``)
and read back at digest time.

The single page renders two independent textareas — one per cadence —
each with the same shape as the original v0.56 weekly editor:

* **Save** — POST ``/settings/digest-prompt`` (weekly) or
  ``/settings/digest-prompt/monthly`` (monthly) with the new template.
  We require every supported placeholder to be present in the body so a
  malformed template can never reach the LLM call site. If a placeholder
  is missing the page re-renders with an inline error and the user's
  in-progress text untouched (the *other* textarea reloads from disk).

* **Reset to default** — POSTs the matching ``/reset`` endpoint which
  stores an empty string. The summariser treats the empty string as
  "fall back to the hard-coded prompt" so the user always has a way to
  return to ground truth without copy-pasting the constant.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.llm.monthly_summariser import (
    MONTHLY_PROMPT_PLACEHOLDERS,
    default_monthly_prompt_template,
)
from app.llm.weekly_summariser import (
    PROMPT_PLACEHOLDERS,
    default_weekly_prompt_template,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.digest.prompt")
_monthly_log = get_logger("persona.digest.monthly_prompt")

_KV_KEY = "weekly_digest_prompt_template"
_MONTHLY_KV_KEY = "monthly_digest_prompt_template"


def _missing_placeholders(template: str, placeholders: tuple[str, ...]) -> list[str]:
    """Return the placeholders that the user forgot to include.

    We do **not** try to parse the template with :class:`string.Formatter`
    — that would reject perfectly valid templates that happen to contain
    a bare ``{`` (e.g. an example JSON block). A simple substring check
    is good enough: ``str.format`` is happy to ignore a placeholder that
    is present in ``**kwargs`` but absent from the template, so we just
    need to make sure every supported placeholder is *mentioned*.
    """
    return [p for p in placeholders if "{" + p + "}" not in template]


async def _render(
    request: Request,
    *,
    weekly_template: str,
    monthly_template: str,
    weekly_error: str | None = None,
    monthly_error: str | None = None,
) -> HTMLResponse:
    """Render the editor page with both textareas populated."""
    return templates.TemplateResponse(
        request,
        "digest_prompts.html",
        {
            "title": "Digest prompts",
            "active_nav": "settings",
            "template": weekly_template,
            "default_template": default_weekly_prompt_template(),
            "placeholders": list(PROMPT_PLACEHOLDERS),
            "is_custom": bool(weekly_template),
            "error": weekly_error,
            "saved": False,
            "monthly_template": monthly_template,
            "monthly_default_template": default_monthly_prompt_template(),
            "monthly_placeholders": list(MONTHLY_PROMPT_PLACEHOLDERS),
            "monthly_is_custom": bool(monthly_template),
            "monthly_error": monthly_error,
        },
    )


async def _load_both() -> tuple[str, str]:
    """Return the current weekly + monthly templates from ``kv_settings``."""
    async with get_connection() as conn:
        weekly = await get_kv(conn, _KV_KEY)
        monthly = await get_kv(conn, _MONTHLY_KV_KEY)
    return (weekly or ""), (monthly or "")


@router.get("/settings/digest-prompt", response_class=HTMLResponse)
async def digest_prompt_page(request: Request) -> HTMLResponse:
    """Render the editor pre-filled with the current values (each may be empty)."""
    weekly, monthly = await _load_both()
    return await _render(
        request,
        weekly_template=weekly,
        monthly_template=monthly,
    )


@router.post("/settings/digest-prompt", response_model=None)
async def digest_prompt_save(
    request: Request,
    template: str = Form(default=""),
) -> HTMLResponse | RedirectResponse:
    """Persist a new weekly template. Re-renders with an error on validation failure."""
    body = template.strip()
    if not body:
        # Empty submission is treated as "reset to default" — same effect
        # as the dedicated reset endpoint, but reachable by simply clearing
        # the textarea and clicking Save.
        async with get_connection() as conn:
            await set_kv(conn, _KV_KEY, "")
        log.info("digest.prompt.cleared")
        return RedirectResponse(url="/settings/digest-prompt", status_code=303)

    missing = _missing_placeholders(body, PROMPT_PLACEHOLDERS)
    if missing:
        log.warning(
            "digest.prompt.rejected",
            missing=missing,
            length=len(body),
        )
        _, monthly = await _load_both()
        return await _render(
            request,
            weekly_template=body,
            monthly_template=monthly,
            weekly_error=(
                "Weekly template is missing required placeholders: "
                + ", ".join("{" + name + "}" for name in missing)
                + ". Add each one exactly as shown and save again."
            ),
        )

    async with get_connection() as conn:
        await set_kv(conn, _KV_KEY, body)
    log.info("digest.prompt.saved", length=len(body))
    return RedirectResponse(url="/settings/digest-prompt", status_code=303)


@router.post("/settings/digest-prompt/reset")
async def digest_prompt_reset(request: Request) -> RedirectResponse:
    """Clear the custom weekly template so the hard-coded default is used again."""
    async with get_connection() as conn:
        await set_kv(conn, _KV_KEY, "")
    log.info("digest.prompt.reset")
    return RedirectResponse(url="/settings/digest-prompt", status_code=303)


@router.post("/settings/digest-prompt/monthly", response_model=None)
async def monthly_digest_prompt_save(
    request: Request,
    template: str = Form(default=""),
) -> HTMLResponse | RedirectResponse:
    """Persist a new monthly template. Re-renders with an error on failure."""
    body = template.strip()
    if not body:
        async with get_connection() as conn:
            await set_kv(conn, _MONTHLY_KV_KEY, "")
        _monthly_log.info("digest.monthly_prompt.cleared")
        return RedirectResponse(url="/settings/digest-prompt", status_code=303)

    missing = _missing_placeholders(body, MONTHLY_PROMPT_PLACEHOLDERS)
    if missing:
        _monthly_log.warning(
            "digest.monthly_prompt.rejected",
            missing=missing,
            length=len(body),
        )
        weekly, _ = await _load_both()
        return await _render(
            request,
            weekly_template=weekly,
            monthly_template=body,
            monthly_error=(
                "Monthly template is missing required placeholders: "
                + ", ".join("{" + name + "}" for name in missing)
                + ". Add each one exactly as shown and save again."
            ),
        )

    async with get_connection() as conn:
        await set_kv(conn, _MONTHLY_KV_KEY, body)
    _monthly_log.info("digest.monthly_prompt.saved", length=len(body))
    return RedirectResponse(url="/settings/digest-prompt", status_code=303)


@router.post("/settings/digest-prompt/monthly/reset")
async def monthly_digest_prompt_reset(request: Request) -> RedirectResponse:
    """Clear the custom monthly template so the hard-coded default is used again."""
    async with get_connection() as conn:
        await set_kv(conn, _MONTHLY_KV_KEY, "")
    _monthly_log.info("digest.monthly_prompt.reset")
    return RedirectResponse(url="/settings/digest-prompt", status_code=303)
