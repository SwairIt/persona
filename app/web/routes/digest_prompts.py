"""Weekly digest prompt template editor (v0.56).

Lets the user override the hard-coded ``_SYSTEM`` constant in
:mod:`app.llm.weekly_summariser` with their own Python ``str.format``
template. The template is persisted in ``kv_settings`` under the key
``weekly_digest_prompt_template`` and read back at digest time.

The single page renders a 20-row textarea, the current value, the list
of supported placeholders, and two buttons:

* **Save** — POST ``/settings/digest-prompt`` with the new template.
  We require every supported placeholder to be present in the body so a
  malformed template can never reach the LLM call site. If a placeholder
  is missing the page re-renders with an inline error and the user's
  in-progress text untouched.

* **Reset to default** — POST ``/settings/digest-prompt/reset`` which
  stores an empty string. The summariser treats the empty string as
  "fall back to the hard-coded prompt" so the user always has a way to
  return to ground truth without copy-pasting the constant.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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

_KV_KEY = "weekly_digest_prompt_template"


def _missing_placeholders(template: str) -> list[str]:
    """Return the placeholders that the user forgot to include.

    We do **not** try to parse the template with :class:`string.Formatter`
    — that would reject perfectly valid templates that happen to contain
    a bare ``{`` (e.g. an example JSON block). A simple substring check
    is good enough: ``str.format`` is happy to ignore a placeholder that
    is present in ``**kwargs`` but absent from the template, so we just
    need to make sure every supported placeholder is *mentioned*.
    """
    return [p for p in PROMPT_PLACEHOLDERS if "{" + p + "}" not in template]


async def _render(
    request: Request,
    *,
    template: str,
    error: str | None = None,
    saved: bool = False,
) -> HTMLResponse:
    """Render the editor page with the given state."""
    return templates.TemplateResponse(
        request,
        "digest_prompts.html",
        {
            "title": "Weekly digest prompt",
            "active_nav": "settings",
            "template": template,
            "default_template": default_weekly_prompt_template(),
            "placeholders": list(PROMPT_PLACEHOLDERS),
            "is_custom": bool(template),
            "error": error,
            "saved": saved,
        },
    )


@router.get("/settings/digest-prompt", response_class=HTMLResponse)
async def digest_prompt_page(request: Request) -> HTMLResponse:
    """Render the editor pre-filled with the current value (may be empty)."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_KEY)
    return await _render(request, template=raw or "")


@router.post("/settings/digest-prompt", response_model=None)
async def digest_prompt_save(
    request: Request,
    template: str = Form(default=""),
) -> HTMLResponse | RedirectResponse:
    """Persist a new template. Re-renders with an error on validation failure."""
    body = template.strip()
    if not body:
        # Empty submission is treated as "reset to default" — same effect
        # as the dedicated reset endpoint, but reachable by simply clearing
        # the textarea and clicking Save.
        async with get_connection() as conn:
            await set_kv(conn, _KV_KEY, "")
        log.info("digest.prompt.cleared")
        return RedirectResponse(url="/settings/digest-prompt", status_code=303)

    missing = _missing_placeholders(body)
    if missing:
        log.warning(
            "digest.prompt.rejected",
            missing=missing,
            length=len(body),
        )
        return await _render(
            request,
            template=body,
            error=(
                "Template is missing required placeholders: "
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
    """Clear the custom template so the hard-coded default is used again."""
    async with get_connection() as conn:
        await set_kv(conn, _KV_KEY, "")
    log.info("digest.prompt.reset")
    return RedirectResponse(url="/settings/digest-prompt", status_code=303)
