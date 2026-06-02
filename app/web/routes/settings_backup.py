"""HTTP routes for downloading / uploading the settings JSON blob.

The page lives at ``/settings/backup``. ``GET`` renders the HTML with
two actions: a "Download backup" button that streams the JSON dump and
an "Upload" form that takes a JSON file plus a ``merge`` vs ``replace``
radio.

The actual export / import logic lives in :mod:`app.settings_backup` —
this module is the thin HTTP shell that streams downloads, parses the
multipart upload, and maps the underlying ``ValueError`` to HTTP 400.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.logging_setup import get_logger
from app.settings_backup import (
    SCHEMA_VERSION,
    export_settings_json,
    import_settings_json,
)
from app.web.templates_engine import templates

log = get_logger("persona.settings_backup")

router = APIRouter(tags=["settings-backup"])

# Cap the upload at 8 MiB — the preference tables together are kilobytes
# in practice, so anything larger is almost certainly a wrong file or a
# DoS attempt. We refuse it before parsing JSON.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024


@router.get("/settings/backup")
async def settings_backup_download() -> Response:
    """Stream the JSON dump as an attachment with a dated filename.

    The HTML page lives at :func:`settings_backup_page` (one level down)
    so this top-level path matches the spec's "GET /settings/backup
    streams JSON download" contract verbatim. The nav link from
    ``/settings`` points at the page, not this endpoint, so casual
    clicks never trigger a download.
    """
    payload = await export_settings_json()
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"persona-settings-{date.today().isoformat()}.json"
    log.info(
        "settings_backup.download",
        bytes=len(body),
        filename=filename,
    )
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Belt-and-braces: never let a browser cache somebody else's
            # preferences blob from a shared proxy.
            "Cache-Control": "no-store",
        },
    )


@router.get("/settings/backup/manage", response_class=HTMLResponse)
async def settings_backup_page(request: Request) -> HTMLResponse:
    """Render the download/upload page."""
    return templates.TemplateResponse(
        request,
        "settings_backup.html",
        {
            "title": "Settings backup",
            "active_nav": "settings",
            "schema_version": SCHEMA_VERSION,
        },
    )


@router.post("/settings/backup/import")
async def settings_backup_import(
    file: Annotated[UploadFile, File(...)],
    merge: Annotated[str, Form()] = "merge",
) -> RedirectResponse:
    """Accept a multipart JSON upload and apply it to the local DB."""
    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid JSON: {exc}"
        ) from exc

    merge_flag = merge != "replace"
    try:
        summary = await import_settings_json(payload, merge=merge_flag)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    written = sum(summary.values())
    log.info(
        "settings_backup.upload",
        merge=merge_flag,
        tables=len(summary),
        rows=written,
        uploaded_at=datetime.now(UTC).isoformat(),
    )
    return RedirectResponse(url="/settings/backup/manage", status_code=303)


__all__ = ["router"]
