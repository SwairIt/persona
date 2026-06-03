"""HTTP route for the weekly digest share-card PNG.

``GET /digest/weekly-archive/{week_start}/card.png`` streams a 1200x630
Open Graph-style preview PNG built by
:func:`app.digest_card.build_weekly_card`. The same path with
``?html=1`` returns a minimal HTML wrapper carrying the ``og:image``
meta tags — useful for testing the preview in social-card validators
without unfurling the full digest detail page.

The generated PNG is written into a per-week temp file so repeat
requests inside the same process reuse the existing file rather than
re-rendering. Cache-busting is intentionally absent — the weekly
digest body changes at most once per week, and a stale card on day 8
is preferable to spending a fraction of a second on PIL for every
unfurl probe.
"""

from __future__ import annotations

import html
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app.digest_card import CARD_HEIGHT, CARD_WIDTH, build_weekly_card
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.digest_card")

router = APIRouter(prefix="/digest", tags=["digest-card"])

# 64 KiB matches the per-day collage route — sweet spot between syscall
# overhead and per-chunk memory pressure on small VMs.
_PNG_CHUNK_BYTES = 64 * 1024


def _validate_week(week_start: str) -> str:
    """Reject anything that isn't ``YYYY-MM-DD`` with a 400 instead of a 500."""
    try:
        datetime.strptime(week_start, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid week_start (expected YYYY-MM-DD)"
        ) from exc
    return week_start


def _card_path(week_start: str) -> Path:
    """Return the on-disk path Persona writes this week's card to.

    Keeps the temp tree predictable so repeat hits reuse a single file
    per week. The directory is created lazily by the renderer.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "persona-digest-card"
    return tmp_dir / f"persona-digest-card-{week_start}.png"


def _html_wrapper(week_start: str, card_url: str) -> str:
    """Render a minimal HTML page that previews the card via OG meta tags.

    Every interpolated value is HTML-escaped because ``week_start`` and
    ``card_url`` reach us from the URL — paranoia is cheap and an
    unescaped ``"`` would void the meta attribute.
    """
    safe_week = html.escape(week_start, quote=True)
    safe_card = html.escape(card_url, quote=True)
    title = html.escape(f"Persona — week of {week_start}", quote=True)
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"en\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        f"<title>{title}</title>\n"
        f"<meta property=\"og:title\" content=\"{title}\">\n"
        "<meta property=\"og:type\" content=\"article\">\n"
        f"<meta property=\"og:image\" content=\"{safe_card}\">\n"
        f"<meta property=\"og:image:width\" content=\"{CARD_WIDTH}\">\n"
        f"<meta property=\"og:image:height\" content=\"{CARD_HEIGHT}\">\n"
        "<meta name=\"twitter:card\" content=\"summary_large_image\">\n"
        f"<meta name=\"twitter:title\" content=\"{title}\">\n"
        f"<meta name=\"twitter:image\" content=\"{safe_card}\">\n"
        "</head>\n"
        "<body>\n"
        f"<h1>Persona — week of {safe_week}</h1>\n"
        f"<p><img src=\"{safe_card}\" alt=\"Weekly digest preview\""
        f" width=\"{CARD_WIDTH}\" height=\"{CARD_HEIGHT}\"></p>\n"
        "</body>\n"
        "</html>\n"
    )


@router.get("/weekly-archive/{week_start}/card.png", response_model=None)
async def weekly_card_png(
    week_start: str,
    html_wrapper: int = Query(default=0, alias="html", ge=0, le=1),
) -> StreamingResponse | HTMLResponse:
    """Stream the weekly digest share-card PNG (or its HTML wrapper).

    When ``?html=1`` is supplied the response is a tiny HTML page whose
    ``<head>`` advertises the PNG via ``og:image``. This is the easiest
    way to debug an unfurl in a social-card validator without exposing
    the full digest detail page.
    """
    week_start = _validate_week(week_start)

    if html_wrapper == 1:
        card_url = f"/digest/weekly-archive/{week_start}/card.png"
        return HTMLResponse(_html_wrapper(week_start, card_url))

    out_path = _card_path(week_start)
    result = await build_weekly_card(week_start, out_path)

    if result["status"] == "bad_date":
        # _validate_week should have caught this — defence in depth.
        raise HTTPException(status_code=400, detail="Invalid week_start")
    if result["status"] != "ok" or result["path"] is None:
        log.error(
            "digest_card.unexpected_status",
            week=week_start,
            status=result["status"],
        )
        raise HTTPException(status_code=500, detail="Card render failed")

    png_path = Path(result["path"])

    def _iter_file() -> Iterator[bytes]:
        with png_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PNG_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    filename = f"persona-week-{week_start}.png"
    return StreamingResponse(
        _iter_file(),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(result["size_bytes"]),
            "X-Persona-Card-Week": result["week_start"],
            "X-Persona-Card-Themes": str(len(result["themes"])),
            "X-Persona-Card-Shots": str(result["total_shots"]),
        },
    )


__all__ = ["router"]
