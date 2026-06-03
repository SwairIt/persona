"""HTTP route for the share-collection ZIP export.

``GET /collection/{slug}/export.zip`` streams the archive produced by
:func:`app.share_collection_zip.build_collection_zip`. ``slug`` is the
signed token minted by :mod:`app.web.routes.share_collection` — same
identifier the public viewer at ``/share/collection/{token}`` and the
PDF surface at ``/collection/{slug}/export.pdf`` already accept.

Sibling of :mod:`app.web.routes.share_collection_pdf`. The error
vocabulary is intentionally aligned so both surfaces return the same
HTTP code for the same upstream condition — no surprise mismatches
when a client retries the wrong URL after a 404.

The temp file is deleted in a ``finally`` block once streaming is
complete (or the client aborts), matching
:mod:`app.web.routes.multi_shot_zip`. This route never leaks the
bundle on disk past the lifetime of the response.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.share_collection_zip import build_collection_zip

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.share_collection_zip")

router = APIRouter(tags=["share"])

# 64 KiB chunks — same constant the multi-shot-zip and PDF-export
# routes use. Big enough to keep syscall overhead negligible, small
# enough that one chunk never pins a noticeable amount of RAM per
# active connection.
_CHUNK_BYTES = 64 * 1024


@router.get("/collection/{slug}/export.zip", response_model=None)
async def export_collection_zip(slug: str) -> StreamingResponse:
    """Stream the share-collection ZIP for ``slug``.

    Status branches mirror :class:`app.share_collection_zip.CollectionZipResult`:

    * ``not_found`` → 404.
    * ``expired`` → 403 (mirrors the public viewer + PDF route).
    * ``corrupt`` → 500 (hand-edited row; surfaces the bug instead of
      hiding it behind an empty zip).
    * ``empty`` → 404 — every referenced shot was hard-deleted.
    * ``ok`` → streamed ``application/zip`` attachment, temp file
      removed in the iterator's ``finally``.
    """
    if not slug or "/" in slug:
        # Defensive: slugs come straight from the URL. Anything
        # containing a path separator can't be a legitimate signed
        # token and would otherwise pollute the temp filename below.
        raise HTTPException(status_code=400, detail="Invalid slug")

    # ``delete=False`` because the file must outlive the ``with`` block —
    # FastAPI hasn't started streaming yet. We unlink it manually in the
    # iterator's ``finally`` so a client disconnect mid-stream still
    # cleans up.
    with tempfile.NamedTemporaryFile(
        prefix=f"persona-share-collection-{slug}-",
        suffix=".zip",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)

    try:
        result = await build_collection_zip(slug, tmp_path)
    except Exception:
        # Any unexpected blow-up must not leave a stray temp file
        # behind. The build helper is defensive, but a fresh bug in a
        # transitive dep should still clean up here.
        tmp_path.unlink(missing_ok=True)
        raise

    if result["status"] != "ok":
        # No file was written for any non-ok status, but the temp shell
        # we created above does exist — remove it before raising.
        tmp_path.unlink(missing_ok=True)
        if result["status"] == "not_found":
            raise HTTPException(
                status_code=404,
                detail="Collection not found",
            )
        if result["status"] == "expired":
            raise HTTPException(status_code=403, detail="Collection expired")
        if result["status"] == "corrupt":
            log.error("share_collection_zip.corrupt_row", slug=slug)
            raise HTTPException(
                status_code=500,
                detail="Collection data corrupt",
            )
        if result["status"] == "empty":
            raise HTTPException(
                status_code=404,
                detail="No screenshots remain in this collection",
            )
        log.error(
            "share_collection_zip.unexpected_status",
            slug=slug,
            status=result["status"],
        )
        raise HTTPException(status_code=500, detail="ZIP export failed")

    # ``path`` is guaranteed non-None when ``status == "ok"``, but mypy
    # can't infer that from a TypedDict — assert defensively.
    assert result["path"] is not None
    zip_path = Path(result["path"])
    size_bytes = int(result["size_bytes"])

    async def _iter_file() -> AsyncIterator[bytes]:
        """Yield ``_CHUNK_BYTES`` chunks then delete the temp file."""
        try:
            with zip_path.open("rb") as fh:
                while True:
                    chunk = await anyio.to_thread.run_sync(
                        fh.read, _CHUNK_BYTES
                    )
                    if not chunk:
                        break
                    yield chunk
        finally:
            # ``suppress(FileNotFoundError)`` because a paranoid caller
            # might rerun the same export concurrently and race us to
            # the unlink.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(zip_path)

    filename = f"persona-share-collection-{slug}.zip"
    log.info(
        "share_collection_zip.download",
        slug=slug,
        path=str(zip_path),
        shots=int(result["shots_count"]),
        thumbnails=int(result["thumbnails_count"]),
        ocr_files=int(result["ocr_count"]),
        size_bytes=size_bytes,
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
