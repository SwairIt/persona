"""Admin page for the colon-command palette mode.

Renders :data:`app.palette_commands.CATALOGUE` as a reference table
plus the snippet a user can paste into DevTools (or wire from their
own template) to load :file:`/static/palette_command_mode.js`. We
deliberately do **not** touch :file:`base.html` so the feature stays
opt-in until the operator decides to ship it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.palette_commands import CATALOGUE
from app.web.templates_engine import templates

log = get_logger("persona.palette_commands.admin")

router = APIRouter(tags=["palette-commands"])


@router.get("/admin/palette-commands", response_class=HTMLResponse)
async def palette_commands_admin(request: Request) -> HTMLResponse:
    """Render the catalogue + enable-instructions page."""
    log.debug("palette_commands.admin.view", count=len(CATALOGUE))
    return templates.TemplateResponse(
        request,
        "palette_commands_admin.html",
        {
            "title": "Palette commands",
            "active_nav": "settings",
            "commands": list(CATALOGUE),
        },
    )
