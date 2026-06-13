"""Public-facing landing page — the product's main page.

Routes:
    * ``GET /``        — home. Logged-in → /now (cabinet). Logged-out → landing.
    * ``GET /landing`` — always renders the landing (even when signed in it
      shows a "you're already signed in as X — continue?" state, per the
      product spec), so a shared marketing link works for everyone.

The page is a standalone marketing template (own design, does NOT extend
base.html) with a Three.js hero, scroll-driven animations and SEO meta.
It is in the auth-gate public allow-list so search engines and logged-out
visitors can see it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import __version__, blog
from app.auth import current_user_optional
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["landing"])
log = get_logger("persona.landing")


def _render(request: Request, session: SessionRecord | None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "title": "Persona",
            "active_nav": "",
            "app_version": __version__,
            "session": session,
            "posts": blog.list_posts()[:3],
        },
    )


@router.get("/", response_class=HTMLResponse, response_model=None)
async def home(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse | RedirectResponse:
    """Home: signed-in users go to the cabinet, everyone else sees the landing."""
    if session is not None:
        return RedirectResponse(url="/now", status_code=303)
    return _render(request, None)


@router.get("/landing", response_class=HTMLResponse, response_model=None)
async def landing_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    """Always render the landing; auth-aware CTA (continue-as-X when signed in)."""
    return _render(request, session)
