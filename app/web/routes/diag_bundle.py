"""HTTP route that streams a diagnostics ``.zip`` for bug reports.

``GET /admin/diagnostics-bundle.zip`` builds a fresh bundle via
:func:`app.diagnostics_bundle.build_diag_bundle` and streams it to the
caller with an ``attachment`` ``Content-Disposition`` so the browser
saves it instead of trying to render it.

Hard rules:

* The temp file is deleted in a ``finally`` block once streaming is
  complete (or the client aborts). The route never leaks the bundle
  on disk past the lifetime of the response.
* Every blocking call (zip build, ``stat``, file ``read``) goes
  through :func:`anyio.to_thread.run_sync` so the event loop stays
  responsive.
* The bundle is regenerated per-request and ``Cache-Control: no-store``
  is set: each download must reflect the current state of the install,
  and a shared proxy must never hand one user's diagnostics to another.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.diagnostics_bundle import build_diag_bundle
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.diag")

router = APIRouter(tags=["diagnostics-bundle"])

# 64 KiB chunks — same constant the archive-bundle route uses. Big
# enough to keep syscall overhead negligible, small enough that one
# chunk doesn't pin a noticeable amount of RAM per active connection.
_CHUNK_BYTES = 64 * 1024


@router.get("/admin/diagnostics-bundle.zip", response_model=None)
async def export_diagnostics_bundle() -> StreamingResponse:
    """Stream ``persona-diag-<DATE>.zip`` to the client.

    No query parameters — the bundle is deliberately uniform so two
    bug reports filed minutes apart are byte-comparable on the
    triage side, modulo timestamps inside ``recent_audit.json``.
    """
    today_iso = date.today().isoformat()

    # ``delete=False`` because the file must outlive the ``with`` block —
    # FastAPI has not started streaming yet. We unlink it manually in
    # the iterator's ``finally`` so a client disconnect mid-stream
    # still cleans up.
    with tempfile.NamedTemporaryFile(
        prefix=f"persona-diag-{today_iso}-",
        suffix=".zip",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)

    try:
        result = await build_diag_bundle(tmp_path)
    except Exception:
        # Any failure leaves the temp file orphaned otherwise — clean
        # up before re-raising so the FastAPI error handler sees the
        # original exception, not an IO follow-up failure.
        tmp_path.unlink(missing_ok=True)
        raise

    size_bytes = int(result["size_bytes"])

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

    filename = f"persona-diag-{today_iso}.zip"
    log.info(
        "diag.bundle.download",
        path=str(tmp_path),
        size_bytes=size_bytes,
        filename=filename,
    )
    return StreamingResponse(
        _iter_file(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size_bytes),
            # Each download builds a fresh bundle — never let a shared
            # proxy hand somebody else's diagnostics to the next client.
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
