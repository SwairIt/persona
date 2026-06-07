"""Public-facing landing page for un-authenticated visitors.

Lives at /landing (kept separate from the existing /welcome page which is
the onboarding wizard for new local installs — different audience).

Why a route file:
    The page is intentionally bare — logo, single tagline, two CTA buttons
    that point at /auth/login and /auth/signup. Those auth routes do not
    exist yet (planned for the next tick); the buttons here render as
    stable href targets so QA can already click through and confirm the
    visual chrome, while the backend evolves.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import __version__
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["landing"])
log = get_logger("persona.landing")


@router.get("/landing", response_class=HTMLResponse)
async def landing_page(request: Request) -> HTMLResponse:
    """Render the public landing page."""
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "title": "Persona",
            "active_nav": "",
            "app_version": __version__,
        },
    )
