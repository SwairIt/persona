"""HTTP route for the per-day collage PNG.

``GET /export/collage.png?day=YYYY-MM-DD`` streams the PNG produced by
:func:`app.day_collage.build_day_collage`. Defaults to today when ``day``
is omitted so the route doubles as a one-click shareable link.

The generated file is written into a temp directory keyed by the day —
identical requests within the same process therefore reuse the same on-disk
path, which keeps the temp tree from growing without bound while the
server is up.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.day_collage import build_day_collage
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.collage")

router = APIRouter(prefix="/export", tags=["collage"])

# 64 KiB matches the per-day PDF route — it's the sweet spot between
# syscall overhead and per-chunk memory pressure on small VMs.
_PNG_CHUNK_BYTES = 64 * 1024


def _today_iso() -> str:
    return date.today().isoformat()


def _validate_day(day: str) -> str:
    """Reject anything that isn't ``YYYY-MM-DD`` with a 400 instead of a 500."""
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid day (expected YYYY-MM-DD)"
        ) from exc
    return day


@router.get("/collage.png", response_model=None)
async def export_day_collage_route(
    day: str = Query(default_factory=_today_iso),
    cols: int = Query(default=4, ge=1, le=12),
    max_shots: int = Query(default=24, ge=1, le=144),
    tile_size: int = Query(default=320, ge=64, le=1024),
) -> StreamingResponse:
    """Stream the per-day collage PNG (defaults to today).

    The query parameters mirror :func:`app.day_collage.build_day_collage`
    so power users can dial the grid via URL — e.g. a 3x9 phone-share
    poster with ``?cols=3&max_shots=27&tile_size=480``. Limits are clamped
    server-side so a hostile client cannot ask for a 100 MP image.
    """
    day = _validate_day(day)

    tmp_dir = Path(tempfile.gettempdir()) / "persona-collage"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Include the grid params in the filename so simultaneous requests for
    # different shapes do not clobber each other's output.
    out_path = (
        tmp_dir
        / f"persona-collage-{day}-{cols}x{tile_size}-n{max_shots}.png"
    )

    result = await build_day_collage(
        day,
        out_path,
        cols=cols,
        max_shots=max_shots,
        tile_size=tile_size,
    )

    if result["status"] == "bad_date":
        # _validate_day should have caught this — defence in depth.
        raise HTTPException(status_code=400, detail="Invalid day")
    if result["status"] == "bad_args":
        raise HTTPException(status_code=400, detail="Invalid collage parameters")
    if result["status"] == "empty":
        raise HTTPException(
            status_code=404, detail=f"No thumbnailed screenshots for {day}"
        )
    if result["status"] != "ok" or result["path"] is None:
        log.error("collage.unexpected_status", day=day, status=result["status"])
        raise HTTPException(status_code=500, detail="Collage export failed")

    png_path = Path(result["path"])

    def _iter_file() -> Iterator[bytes]:
        with png_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PNG_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    filename = f"persona-collage-{day}.png"
    return StreamingResponse(
        _iter_file(),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(result["size_bytes"]),
            "X-Persona-Collage-Cols": str(result["cols"]),
            "X-Persona-Collage-Rows": str(result["rows"]),
            "X-Persona-Collage-Shots": str(result["shots_used"]),
        },
    )


__all__ = ["router"]
