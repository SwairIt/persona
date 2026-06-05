"""HTTP routes for the per-insight share cards.

Two endpoints:

* ``GET /share/insight/{kind}.png`` — streams one of the four PNG
  templates produced by :mod:`app.insight_cards`. ``kind`` is one of
  ``top_app``, ``longest_focus``, ``most_active_hour`` or ``streak``;
  anything else returns ``404``.
* ``GET /share/insights`` — gallery HTML that embeds all four cards in
  a 2x2 grid with per-card Copy-URL / Open-PNG buttons.

The ``week`` and ``day`` query strings are validated with
``datetime.strptime`` so a malformed value returns ``400`` instead of
silently rendering "this week" / "today".
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.insight_cards import (
    CARD_HEIGHT,
    CARD_WIDTH,
    build_longest_focus_card,
    build_most_active_hour_card,
    build_streak_card,
    build_top_app_card,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

log = get_logger("persona.insight_cards")

router = APIRouter(prefix="/share", tags=["insight-cards"])

# One-minute public cache — the same TTL as the dashboard card; long
# enough to soak up an unfurl probe, short enough that fresh captures
# show up promptly when the operator refreshes the gallery.
_CACHE_CONTROL: Final[str] = "public, max-age=60"

# Public surface for the supported ``kind`` slugs. The mapping ties
# each slug to its async builder so the dispatcher stays a one-line
# lookup instead of a long ``if/elif`` chain.
_KIND_BUILDERS: Final[dict[str, Callable[[str | None], Awaitable[bytes]]]] = {
    "top_app": build_top_app_card,
    "longest_focus": build_longest_focus_card,
    # The two below ignore the optional date argument; we still wrap
    # them so every value in the dict has the same callable signature.
    "most_active_hour": lambda _value: build_most_active_hour_card(),
    "streak": lambda _value: build_streak_card(),
}

# Human-friendly card titles for the gallery template — keeps the
# Jinja template free of string-translation logic.
_KIND_TITLES: Final[dict[str, str]] = {
    "top_app": "Top app this week",
    "longest_focus": "Longest focus session",
    "most_active_hour": "Most productive hour",
    "streak": "Daily capture streak",
}


def _validate_iso_day(value: str | None, *, field: str) -> str | None:
    """Reject anything that isn't ``YYYY-MM-DD`` with a 400.

    ``None`` (omitted query string) passes through untouched so the
    underlying builder can resolve its own default.
    """
    if value is None or value == "":
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} (expected YYYY-MM-DD)",
        ) from exc
    return value


@router.get("/insight/{kind}.png", response_model=None)
async def insight_card_png_endpoint(
    kind: str,
    week: str | None = Query(default=None),
    day: str | None = Query(default=None),
) -> Response:
    """Stream a single insight card as ``image/png``.

    Args:
        kind: One of ``top_app``, ``longest_focus``,
            ``most_active_hour``, ``streak``.
        week: Optional ``YYYY-MM-DD`` anchor for the ``top_app`` card;
            ignored by the other kinds.
        day: Optional ``YYYY-MM-DD`` anchor for the ``longest_focus``
            card; ignored by the other kinds.

    Returns:
        ``image/png`` with a 60-second public cache hint. ``404`` on
        an unknown ``kind``; ``503`` when Pillow is missing.
    """
    builder = _KIND_BUILDERS.get(kind)
    if builder is None:
        log.warning("insight_cards.unknown_kind", kind=kind)
        raise HTTPException(status_code=404, detail="Unknown insight kind")

    if kind == "top_app":
        argument = _validate_iso_day(week, field="week")
    elif kind == "longest_focus":
        argument = _validate_iso_day(day, field="day")
    else:
        argument = None

    png_bytes = await builder(argument)
    if not png_bytes:
        log.error("insight_cards.empty_payload", kind=kind)
        raise HTTPException(
            status_code=503,
            detail="Insight card renderer unavailable",
        )

    filename = f"persona-insight-{kind}.png"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(png_bytes)),
            "Cache-Control": _CACHE_CONTROL,
            "X-Persona-Card-Kind": kind,
            "X-Persona-Card-Width": str(CARD_WIDTH),
            "X-Persona-Card-Height": str(CARD_HEIGHT),
        },
    )


@router.get("/insights", response_class=HTMLResponse)
async def insight_cards_gallery(request: Request) -> HTMLResponse:
    """Render the 2x2 gallery of all four insight cards."""
    cards = [
        {
            "kind": kind,
            "title": _KIND_TITLES[kind],
            "png_url": f"/share/insight/{kind}.png",
        }
        for kind in ("top_app", "longest_focus", "most_active_hour", "streak")
    ]
    return templates.TemplateResponse(
        request,
        "insight_cards_gallery.html",
        {
            "title": "Поделиться",
            "active_nav": "stats",
            "cards": cards,
            "card_width": CARD_WIDTH,
            "card_height": CARD_HEIGHT,
        },
    )


__all__ = ["router"]
