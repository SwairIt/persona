"""HTTP route that streams a Persona archive bundle as a ``.zip``.

``GET /export/archive.zip?days=7&thumbs=1`` builds an archive via
:func:`app.archive_bundle.build_archive` and streams the resulting file
back to the caller. The archive is materialised to disk first because
it can grow into the tens of megabytes (one WebP per screenshot) — too
big to comfortably keep in a ``BytesIO`` on a small laptop.

Hard rules:

* The temp file is deleted in a ``finally`` block once streaming is
  complete (or the client aborts). This route never leaks the archive
  on disk past the lifetime of the response.
* Every blocking call (zip build, stat, file ``read``) goes through
  :func:`anyio.to_thread.run_sync` so the event loop stays responsive.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.archive_bundle import build_archive
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.archive")

router = APIRouter(prefix="/export", tags=["archive-bundle"])

# 64 KiB chunks — same constant the PDF export uses. Big enough to keep
# syscall overhead negligible, small enough that one chunk doesn't pin
# a noticeable amount of RAM per active connection.
_CHUNK_BYTES = 64 * 1024

# Cap the lookback at one year so a stray ``?days=999999`` doesn't try
# to bundle every thumbnail Persona has ever written. The encrypted
# snapshot tool is the right answer for "everything", not this route.
_MAX_DAYS = 366


@router.get("/archive.zip", response_model=None)
async def export_archive_zip(
    days: int = Query(default=7, ge=1, le=_MAX_DAYS),
    thumbs: int = Query(default=1, ge=0, le=1),
) -> StreamingResponse:
    """Stream a ``persona-archive-<N>d.zip`` to the client.

    Args:
        days: Lookback window. Clamped by ``Query`` to ``[1, 366]``.
        thumbs: ``1`` (default) includes thumbnails; ``0`` skips them.
            Integer rather than bool so the URL stays ergonomic
            (``?thumbs=0`` reads better than ``?thumbs=false``).
    """
    include_thumbnails = bool(thumbs)

    # ``delete=False`` because the file must outlive the ``with`` block —
    # FastAPI hasn't started streaming yet. We unlink it manually in the
    # iterator's ``finally`` so a client disconnect mid-stream still
    # cleans up.
    with tempfile.NamedTemporaryFile(
        prefix=f"persona-archive-{date.today().isoformat()}-",
        suffix=".zip",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)

    try:
        result = await build_archive(
            days=days,
            output_path=tmp_path,
            include_thumbnails=include_thumbnails,
        )
    except ValueError as exc:
        # ``Query`` constraints already reject the obvious bad input,
        # but ``build_archive`` is the source of truth. Map any future
        # validation error to a clean 400 so callers see the message.
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    size_bytes = int(result["size_bytes"])
    files_count = int(result["files_count"])

    async def _iter_file() -> AsyncIterator[bytes]:
        """Yield ``_CHUNK_BYTES`` chunks then delete the temp file."""
        try:
            with tmp_path.open("rb") as fh:
                while True:
                    chunk = await anyio.to_thread.run_sync(fh.read, _CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk
        finally:
            # ``suppress(FileNotFoundError)`` because a paranoid caller
            # might rerun the same export concurrently and race us to
            # the unlink.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)

    filename = f"persona-archive-{days}d.zip"
    log.info(
        "archive.download",
        path=str(tmp_path),
        days=days,
        size_bytes=size_bytes,
        files_count=files_count,
        include_thumbnails=include_thumbnails,
    )
    return StreamingResponse(
        _iter_file(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size_bytes),
            # Each download builds a fresh archive — never let a shared
            # proxy hand somebody else's screenshots to the next client.
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
