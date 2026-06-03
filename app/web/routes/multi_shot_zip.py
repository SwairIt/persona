"""HTTP route that streams a hand-picked multi-shot share as a ``.zip``.

``POST /api/multi-shot-zip`` accepts a JSON body ``{"ids": [...]}`` (up
to :data:`app.multi_shot_zip.MAX_IDS` entries), builds a bundle via
:func:`app.multi_shot_zip.build_shots_zip` into a tempfile, and streams
the resulting archive back to the caller.

Hard rules:

* The temp file is deleted in a ``finally`` block once streaming is
  complete (or the client aborts). This route never leaks the bundle
  on disk past the lifetime of the response.
* Every blocking call (zip build, ``stat``, file ``read``) goes through
  :func:`anyio.to_thread.run_sync` so the event loop stays responsive.
* Request body uses pydantic v2 strict typing so a stray string id like
  ``"7"`` or a negative number is rejected with a clean 422 — we never
  silently coerce.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.logging_setup import get_logger
from app.multi_shot_zip import MAX_IDS, build_shots_zip

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.multi_shot_zip")

router = APIRouter(tags=["multi-shot-zip"])

# 64 KiB chunks — same constant the archive-bundle route uses. Big
# enough to keep syscall overhead negligible, small enough that one
# chunk never pins a noticeable amount of RAM per active connection.
_CHUNK_BYTES = 64 * 1024


class MultiShotZipRequest(BaseModel):
    """Body for ``POST /api/multi-shot-zip``.

    Just one field. ``extra="forbid"`` so a typo like ``{"id": [...]}``
    surfaces a 422 instead of silently producing an empty zip.
    """

    model_config = ConfigDict(extra="forbid")

    ids: Annotated[
        list[Annotated[int, Field(ge=1)]],
        Field(min_length=1, max_length=MAX_IDS),
    ]


@router.post("/api/multi-shot-zip", response_model=None)
async def post_multi_shot_zip(payload: MultiShotZipRequest) -> StreamingResponse:
    """Stream a ``persona-shots-<DATE>.zip`` to the client.

    The bundle contains a ``manifest.json`` + one ``thumbnails/<id>.webp``
    per resolved shot + one ``ocr/<id>.txt`` per shot (OCR text passed
    through the user's redaction rules before write).
    """
    # ``delete=False`` because the file must outlive the ``with`` block —
    # FastAPI hasn't started streaming yet. We unlink it manually in the
    # iterator's ``finally`` so a client disconnect mid-stream still
    # cleans up.
    iso_date = date.today().isoformat()
    with tempfile.NamedTemporaryFile(
        prefix=f"persona-shots-{iso_date}-",
        suffix=".zip",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)

    try:
        result = await build_shots_zip(
            shot_ids=list(payload.ids),
            output_path=tmp_path,
        )
    except ValueError as exc:
        # Pydantic already enforces ``max_length`` so we should never
        # see ValueError here in practice — but the library is the
        # source of truth, so any future validation error becomes a
        # clean 400 instead of a 500.
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    size_bytes = int(result["size_bytes"])
    shots_count = int(result["shots_count"])
    thumbnails_count = int(result["thumbnails_count"])
    ocr_count = int(result["ocr_count"])

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

    filename = f"persona-shots-{iso_date}.zip"
    log.info(
        "multi_shot_zip.download",
        path=str(tmp_path),
        requested=len(payload.ids),
        resolved=shots_count,
        thumbnails=thumbnails_count,
        ocr_files=ocr_count,
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


__all__ = ["MultiShotZipRequest", "router"]
