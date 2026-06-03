"""HTTP route for the monthly digest share-card PNG.

``GET /digest/monthly-archive/{month}/card.png`` streams a 1200x630
Open Graph-style preview PNG built by
:func:`app.monthly_digest_card.build_monthly_card`. The same path with
``?html=1`` returns a minimal HTML wrapper carrying the ``og:image``
meta tags — useful for testing the preview in social-card validators
without unfurling the full digest detail page.

The generated PNG is written into a per-month temp file so repeat
requests inside the same process reuse the existing file rather than
re-rendering. Cache-busting is intentionally absent — the monthly
digest body changes at most once per month, and a stale card on day 32
is preferable to spending a fraction of a second on PIL for every
unfurl probe.

This is the monthly twin of :mod:`app.web.routes.digest_card`; the two
routes share neither code nor temp dir on purpose so a regression in
one cannot poison the cache of the other.
"""

from __future__ import annotations

import html
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app.logging_setup import get_logger
from app.monthly_digest_card import CARD_HEIGHT, CARD_WIDTH, build_monthly_card

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.monthly_card")

router = APIRouter(prefix="/digest", tags=["digest-card"])

# 64 KiB matches the weekly digest card route — sweet spot between
# syscall overhead and per-chunk memory pressure on small VMs.
_PNG_CHUNK_BYTES = 64 * 1024


def _validate_month(month: str) -> str:
    """Reject anything that isn't ``YYYY-MM`` with a 400 instead of a 500.

    Mirrors :func:`app.web.routes.monthly_digests._is_valid_month` but
    raises ``HTTPException`` directly so the streaming response never
    sees a malformed input.
    """
    parts = month.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise HTTPException(
            status_code=400, detail="Invalid month (expected YYYY-MM)"
        )
    if not (parts[0].isdigit() and parts[1].isdigit()):
        raise HTTPException(
            status_code=400, detail="Invalid month (expected YYYY-MM)"
        )
    month_num = int(parts[1])
    if not 1 <= month_num <= 12:
        raise HTTPException(
            status_code=400, detail="Invalid month (1..12)"
        )
    return month


def _card_path(month: str) -> Path:
    """Return the on-disk path Persona writes this month's card to.

    Keeps the temp tree predictable so repeat hits reuse a single file
    per month. The directory is created lazily by the renderer.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "persona-monthly-digest-card"
    return tmp_dir / f"persona-monthly-digest-card-{month}.png"


def _html_wrapper(month: str, card_url: str) -> str:
    """Render a minimal HTML page that previews the card via OG meta tags.

    Every interpolated value is HTML-escaped because ``month`` and
    ``card_url`` reach us from the URL — paranoia is cheap and an
    unescaped ``"`` would void the meta attribute.
    """
    safe_month = html.escape(month, quote=True)
    safe_card = html.escape(card_url, quote=True)
    title = html.escape(f"Persona — month of {month}", quote=True)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        f"<title>{title}</title>\n"
        f'<meta property="og:title" content="{title}">\n'
        '<meta property="og:type" content="article">\n'
        f'<meta property="og:image" content="{safe_card}">\n'
        f'<meta property="og:image:width" content="{CARD_WIDTH}">\n'
        f'<meta property="og:image:height" content="{CARD_HEIGHT}">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{title}">\n'
        f'<meta name="twitter:image" content="{safe_card}">\n'
        "</head>\n"
        "<body>\n"
        f"<h1>Persona — month of {safe_month}</h1>\n"
        f'<p><img src="{safe_card}" alt="Monthly digest preview"'
        f' width="{CARD_WIDTH}" height="{CARD_HEIGHT}"></p>\n'
        "</body>\n"
        "</html>\n"
    )


@router.get("/monthly-archive/{month}/card.png", response_model=None)
async def monthly_card_png(
    month: str,
    html_wrapper: int = Query(default=0, alias="html", ge=0, le=1),
) -> StreamingResponse | HTMLResponse:
    """Stream the monthly digest share-card PNG (or its HTML wrapper).

    When ``?html=1`` is supplied the response is a tiny HTML page whose
    ``<head>`` advertises the PNG via ``og:image``. This is the easiest
    way to debug an unfurl in a social-card validator without exposing
    the full digest detail page.
    """
    month = _validate_month(month)

    if html_wrapper == 1:
        card_url = f"/digest/monthly-archive/{month}/card.png"
        return HTMLResponse(_html_wrapper(month, card_url))

    out_path = _card_path(month)
    result = await build_monthly_card(month, out_path)

    if result["status"] == "bad_date":
        # _validate_month should have caught this — defence in depth.
        raise HTTPException(status_code=400, detail="Invalid month")
    if result["status"] != "ok" or result["path"] is None:
        log.error(
            "monthly_card.unexpected_status",
            month=month,
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

    filename = f"persona-month-{month}.png"
    return StreamingResponse(
        _iter_file(),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(result["size_bytes"]),
            "X-Persona-Card-Month": result["month"],
            "X-Persona-Card-Themes": str(len(result["themes"])),
            "X-Persona-Card-Shots": str(result["total_shots"]),
            "X-Persona-Card-Days": str(result["days_in_month"]),
        },
    )


__all__ = ["router"]
