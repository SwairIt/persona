"""HTTP route for the visual diff thumbnail PNG.

``GET /api/diff/{a}/{b}/thumb.png`` streams the 320x180 pixel-diff PNG
produced by :func:`app.visual_diff.generate_diff_thumbnail`. The diff is
deterministic in ``(a, b)`` so we serve it with a long ``Cache-Control:
public, max-age=86400, immutable`` — browsers can park it for the day,
and repeated requests inside one process land on the same temp file.

Returns 404 if either screenshot row is missing or its thumbnail cannot
be located on disk; the route never half-renders.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.logging_setup import get_logger
from app.visual_diff import generate_diff_thumbnail

log = get_logger("persona.visual_diff")

router = APIRouter(prefix="/api/diff", tags=["analysis"])

# 24h, immutable: the diff is a pure function of (a, b)'s pixel grids, so
# the answer never changes for a fixed pair. The same reasoning is what
# lets us reuse a process-scoped temp file as a poor-man's cache.
_CACHE_CONTROL: str = "public, max-age=86400, immutable"


def _temp_output_path(shot_a_id: int, shot_b_id: int) -> Path:
    """Stable temp path keyed by ``(a, b)`` so identical requests reuse the file."""
    tmp_dir = Path(tempfile.gettempdir()) / "persona-visual-diff"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir / f"diff-{shot_a_id}-{shot_b_id}.png"


@router.get("/{a}/{b}/thumb.png", response_model=None)
async def diff_thumbnail_route(a: int, b: int) -> FileResponse:
    """Serve the visual diff PNG for screenshots ``a`` vs ``b``.

    Generated on demand into a process-scoped temp file. The first hit
    pays the PIL cost (two reads, two resizes, one difference, one
    contrast enhance, one PNG write); subsequent hits inside the same
    process reuse the file via :class:`FileResponse`'s stat-based 304
    handling, and external callers also benefit from the immutable
    ``Cache-Control`` we emit.
    """
    out_path = _temp_output_path(a, b)

    result = await generate_diff_thumbnail(a, b, out_path)

    if result["status"] == "missing" or result["path"] is None:
        log.info("visual_diff.route.missing", id_a=a, id_b=b)
        raise HTTPException(status_code=404, detail="Screenshot not found")

    if result["status"] != "ok":
        # Defence in depth — generate_diff_thumbnail currently only ever
        # returns "ok" or "missing", but a future status should fail
        # closed rather than silently 200.
        log.error(
            "visual_diff.route.unexpected_status",
            id_a=a,
            id_b=b,
            status=result["status"],
        )
        raise HTTPException(status_code=500, detail="Diff render failed")

    return FileResponse(
        Path(result["path"]),
        media_type="image/png",
        headers={"Cache-Control": _CACHE_CONTROL},
    )


__all__ = ["router"]
