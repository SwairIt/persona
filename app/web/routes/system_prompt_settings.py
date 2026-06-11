"""T29 — chat system-prompt picker/editor.

Lets the user choose a base system prompt for chat (default + curated
presets adapted from Anthropic's Claude Code prompts), edit the full text
freely, save it, or reset to default. The saved prompt is the global
default for ALL models; a per-session "роль" still overrides it.

Mirrors the /settings/digest-prompt pattern: kv-stored text, empty =
fall back to the hard-coded default.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.chat import (
    DEFAULT_SYSTEM_PROMPT,
    PRESETS,
    get_active_system_prompt,
    is_custom_system_prompt,
    reset_active_system_prompt,
    set_active_system_prompt,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.chat.prompt_settings")


async def _render(request: Request, *, saved: bool) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "system_prompt_settings.html",
        {
            "title": "Системный промпт",
            "active_nav": "settings",
            "presets": PRESETS,
            "active_text": await get_active_system_prompt(),
            "is_custom": await is_custom_system_prompt(),
            "default_text": DEFAULT_SYSTEM_PROMPT,
            "saved": saved,
        },
    )


@router.get("/settings/system-prompt", response_class=HTMLResponse)
async def system_prompt_page(request: Request) -> HTMLResponse:
    return await _render(request, saved=False)


@router.post("/settings/system-prompt", response_class=HTMLResponse, response_model=None)
async def system_prompt_save(
    request: Request,
    prompt_text: str = Form(default=""),
) -> HTMLResponse:
    body = (prompt_text or "").strip()
    # Empty (or identical to default) → treat as reset so the user always
    # has a clean way back to ground truth.
    if body and body != DEFAULT_SYSTEM_PROMPT.strip():
        await set_active_system_prompt(body)
        log.info("chat.system_prompt.saved", length=len(body))
    else:
        await reset_active_system_prompt()
        log.info("chat.system_prompt.reset_via_save")
    return await _render(request, saved=True)


@router.post("/settings/system-prompt/reset", response_model=None)
async def system_prompt_reset(request: Request) -> RedirectResponse:
    await reset_active_system_prompt()
    log.info("chat.system_prompt.reset")
    return RedirectResponse(url="/settings/system-prompt", status_code=303)
