"""Walkthrough page that explains where to click in Persona.

User asked for a single readable page that says: "here is what every part
of the site does, where to start, how to test, what to NOT touch." The
intended reader is the project owner returning after a long autonomous
build run, not a third-party visitor — so the tone is direct and skips
marketing copy.

Route lives at /help and is linked from:
  * the bottom-right help widget on every page (template ``_help_widget.html``)
  * the iOS PWA manifest shortcut (long-press the home-screen icon)

The route reads the current app version and a few live stats so the page
shows the user *their* numbers, not template placeholders.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import __version__
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["help"])
log = get_logger("persona.help_walkthrough")


async def _live_stats() -> dict[str, int]:
    """Return small numbers we surface on /help so the page feels live."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
        row = await cursor.fetchone()
        total_shots = int(row["n"]) if row else 0

        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= datetime('now', '-1 day')"
        )
        row = await cursor.fetchone()
        last_24h = int(row["n"]) if row else 0

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshot_notes")
        row = await cursor.fetchone()
        total_notes = int(row["n"]) if row else 0

    return {
        "total_shots": total_shots,
        "last_24h": last_24h,
        "total_notes": total_notes,
    }


@router.get("/help", response_class=HTMLResponse)
async def help_walkthrough(request: Request) -> HTMLResponse:
    """Render the walkthrough page."""
    stats = await _live_stats()
    return templates.TemplateResponse(
        request,
        "help_walkthrough.html",
        {
            "title": "Справка",
            "active_nav": "",
            "stats": stats,
            "app_version": __version__,
        },
    )
