"""HTTP routes for the dashboard day-snapshot PNG share-card.

Exposes two endpoints:

* ``GET /dashboard/card.png?day=YYYY-MM-DD`` — streams the binary PNG
  produced by :func:`app.dashboard_card_png.build_card_png`. The ``day``
  query string is optional; omitting it produces a card for *today*.
* ``GET /dashboard/card`` — an HTML preview page that embeds the PNG
  above and offers a "Copy share URL" button so the operator can paste
  the link into Telegram / Slack / iMessage.

Validation is intentionally strict on the binary endpoint: malformed
``day`` values produce ``400 Bad Request`` rather than silently
rendering today's card. The preview page tolerates the same input and
defers the binary response to the ``<img>`` tag's request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard_card_png import CARD_HEIGHT, CARD_WIDTH, build_card_png
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.dashboard_card")

router = APIRouter(prefix="/dashboard", tags=["dashboard-card"])

# Cache the rendered card for one minute in CDN/proxy layers — long
# enough to absorb the unfurl probes a single Telegram/Slack paste
# generates, short enough that fresh captures show up promptly.
_CACHE_CONTROL: Final[str] = "public, max-age=60"


def _validate_day(day: str | None) -> str | None:
    """Reject anything that isn't ``YYYY-MM-DD`` with a 400.

    ``None`` (omitted query string) passes through untouched so
    :func:`build_card_png` can resolve "today" itself.
    """
    if day is None or day == "":
        return None
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid day (expected YYYY-MM-DD)"
        ) from exc
    return day


@router.get("/card.png", response_model=None)
async def dashboard_card_png_endpoint(
    day: str | None = Query(default=None),
) -> Response:
    """Stream the day-snapshot PNG (binary).

    Args:
        day: ``YYYY-MM-DD`` for the target day. Omit to get today.

    Returns:
        ``image/png`` response with a one-minute public cache hint.
        Returns ``503`` when Pillow is unavailable and ``b""`` came
        back from the renderer.
    """
    validated = _validate_day(day)
    png_bytes = await build_card_png(validated)

    if not png_bytes:
        log.error("dashboard_card.empty_payload", day=validated)
        raise HTTPException(
            status_code=503, detail="Dashboard card renderer unavailable"
        )

    suffix = validated if validated is not None else "today"
    filename = f"persona-dashboard-{suffix}.png"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(png_bytes)),
            "Cache-Control": _CACHE_CONTROL,
            "X-Persona-Card-Day": suffix,
            "X-Persona-Card-Width": str(CARD_WIDTH),
            "X-Persona-Card-Height": str(CARD_HEIGHT),
        },
    )


@router.get("/card", response_class=HTMLResponse)
async def dashboard_card_preview(
    request: Request,
    day: str | None = Query(default=None),
) -> HTMLResponse:
    """Preview page — embeds the PNG and offers a copy-link button."""
    validated = _validate_day(day)
    if validated is not None:
        png_url = f"/dashboard/card.png?day={validated}"
        share_label = validated
    else:
        png_url = "/dashboard/card.png"
        share_label = "today"

    return templates.TemplateResponse(
        request,
        "dashboard_card_preview.html",
        {
            "title": "Dashboard card",
            "active_nav": "stats",
            "png_url": png_url,
            "share_label": share_label,
            "day": validated or "",
            "card_width": CARD_WIDTH,
            "card_height": CARD_HEIGHT,
        },
    )


__all__ = ["router"]
