"""Web search settings — paste a Brave Search API key from anywhere (v2.30.31).

``web_search`` in :mod:`app.mcp.builtin_tools` prefers the Brave API when a
key is configured (``kv_settings['byo_api_key_brave']``) and otherwise falls
back to a keyless provider (DuckDuckGo HTML for web, Openverse for
images/GIFs) — so search always works, even with nothing set up. This page
just lets the owner paste a Brave key later, e.g. from a phone, without
touching the server directly.

Security: the key is NEVER rendered back into the HTML ``value=`` attribute
— this project leaked exactly that once before (see llm_switcher.py for the
established mitigation). The page only ever shows a "key is set" badge.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.web.templates_engine import templates
from app.websearch_settings import clear_brave_key, has_brave_key, save_brave_key

router = APIRouter(tags=["settings"])
log = get_logger("persona.web_search.settings")


@router.get("/settings/web-search", response_class=HTMLResponse)
async def web_search_settings_page(request: Request) -> HTMLResponse:
    """Render the Brave key form with a configured/not-configured badge only."""
    return templates.TemplateResponse(
        request,
        "settings_web_search.html",
        {
            "title": "Поиск в интернете",
            "active_nav": "settings",
            "has_key": await has_brave_key(),
        },
    )


@router.post("/settings/web-search")
async def web_search_settings_save(
    request: Request,
    brave_api_key: str = Form(default=""),
    action: str = Form(default="save"),
) -> RedirectResponse:
    """Save (or clear) the Brave key. An empty field on 'save' is a no-op."""
    key = brave_api_key.strip()
    if action == "clear":
        await clear_brave_key()
        await log_action("web_search.settings", target="brave", detail="cleared", success=True)
        log.info("web_search.settings.cleared")
    elif key:
        await save_brave_key(key)
        await log_action("web_search.settings", target="brave", detail="saved", success=True)
        log.info("web_search.settings.saved")
    return RedirectResponse(url="/settings/web-search", status_code=303)
