"""HTTP route for the weekly-stats share-card PNG.

``GET /stats/weekly-card.png`` streams a 1200x630 social-preview PNG
built by :func:`app.weekly_stats_card.build_weekly_stats_card`. The
card summarises the last seven days of capture activity — total shots,
top app, top three OCR keywords — and is designed to be embedded as the
``og:image`` of any "share your week" page, or simply downloaded by the
user and posted directly to Twitter/X / Telegram / LinkedIn.

The card is generated on demand and written to a single temp file
keyed by ``end_date``. Repeat requests within the same calendar day
hit the existing file instead of re-rendering — the underlying stats
change at most once per minute and we accept a sub-minute staleness in
exchange for a near-zero-cost endpoint.

Cache-busting via ``?refresh=1`` forces a re-render for the same
``end_date``; useful when a user has just imported a backlog and the
stale PNG would mislead viewers.

Query parameters
----------------
end : ``YYYY-MM-DD``
    Optional last day of the recap window. Defaults to today.
refresh : ``0`` or ``1``
    Force re-render even when a cached PNG exists. Defaults to ``0``.
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.weekly_stats_card import (
    CARD_HEIGHT,
    CARD_WIDTH,
    build_weekly_stats_card,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.weekly_stats_card")

router = APIRouter(prefix="/stats", tags=["weekly-stats-card"])

# 64 KiB matches the weekly/monthly digest card routes — sweet spot
# between syscall overhead and per-chunk memory pressure on small VMs.
_PNG_CHUNK_BYTES = 64 * 1024


def _today_iso() -> str:
    """Return today's local date as ``YYYY-MM-DD``.

    Wrapped in a tiny helper so the default-end-date branch is trivial
    to mock in unit tests without monkey-patching ``datetime``.
    """
    return datetime.now().astimezone().date().isoformat()


def _validate_end(end: str) -> str:
    """Reject anything that isn't ``YYYY-MM-DD`` with a 400 instead of a 500."""
    try:
        datetime.strptime(end, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid end (expected YYYY-MM-DD)"
        ) from exc
    return end


def _card_path(end: str) -> Path:
    """Return the on-disk path Persona writes this window's card to.

    Keeps the temp tree predictable so repeat hits reuse a single file
    per ``end_date``. The directory is created lazily by the renderer.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "persona-weekly-stats-card"
    return tmp_dir / f"persona-weekly-stats-{end}.png"


@router.get("/weekly-card.png", response_model=None)
async def weekly_stats_card_png(
    end: str | None = Query(default=None, description="YYYY-MM-DD (default: today)"),
    refresh: int = Query(default=0, ge=0, le=1, description="Force re-render"),
) -> StreamingResponse:
    """Stream the weekly-stats share-card PNG for the requested window.

    The response is always ``image/png`` with ``Content-Disposition:
    inline`` so browsers preview it directly and OG-image scrapers can
    fetch it without a content-type negotiation dance.
    """
    end_iso = _validate_end(end if end is not None else _today_iso())
    out_path = _card_path(end_iso)

    # Reuse the cached PNG only when the file exists *and* the caller
    # has not requested a refresh. Any IO error falls through to the
    # renderer so a partially-written file from a previous crash heals
    # itself on the next request.
    if refresh == 0 and out_path.is_file() and out_path.stat().st_size > 0:
        size_bytes = out_path.stat().st_size
        log.info(
            "weekly_stats_card.cache_hit",
            end_date=end_iso,
            path=str(out_path),
            size_bytes=size_bytes,
        )
        png_path = out_path
        headers = {
            "Content-Disposition": (
                f'inline; filename="persona-weekly-stats-{end_iso}.png"'
            ),
            "Content-Length": str(size_bytes),
            "X-Persona-Card-End": end_iso,
            "X-Persona-Card-Cached": "1",
        }
    else:
        result = await build_weekly_stats_card(end_iso, out_path)
        if result["status"] == "bad_date":
            # ``_validate_end`` should have caught this — defence in depth.
            raise HTTPException(status_code=400, detail="Invalid end date")
        if result["status"] != "ok" or result["path"] is None:
            log.error(
                "weekly_stats_card.unexpected_status",
                end_date=end_iso,
                status=result["status"],
            )
            raise HTTPException(status_code=500, detail="Card render failed")
        png_path = Path(result["path"])
        headers = {
            "Content-Disposition": (
                f'inline; filename="persona-weekly-stats-{end_iso}.png"'
            ),
            "Content-Length": str(result["size_bytes"]),
            "X-Persona-Card-End": result["end_date"],
            "X-Persona-Card-Start": result["start_date"],
            "X-Persona-Card-Shots": str(result["total_shots"]),
            "X-Persona-Card-Apps": str(result["unique_apps"]),
            "X-Persona-Card-Top-App": result["top_app"] or "",
            "X-Persona-Card-Keywords": str(len(result["keywords"])),
            "X-Persona-Card-Cached": "0",
        }

    def _iter_file() -> Iterator[bytes]:
        with png_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PNG_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    # Sanity-headers for clients that want to confirm the card was sized
    # as expected without parsing the PNG itself.
    headers["X-Persona-Card-Width"] = str(CARD_WIDTH)
    headers["X-Persona-Card-Height"] = str(CARD_HEIGHT)

    return StreamingResponse(
        _iter_file(),
        media_type="image/png",
        headers=headers,
    )


__all__ = ["router"]
