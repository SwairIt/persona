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
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.audit import log_action
from app.logging_setup import get_logger
from app.settings_backup import (
    SCHEMA_VERSION,
    export_settings_json,
    import_settings_json,
)
from app.web.templates_engine import templates

log = get_logger("persona.settings_backup")
log_import_url = get_logger("persona.settings.import_url")

router = APIRouter(tags=["settings-backup"])

# Cap the upload at 8 MiB — the preference tables together are kilobytes
# in practice, so anything larger is almost certainly a wrong file or a
# DoS attempt. We refuse it before parsing JSON.
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

# Cap for the remote URL import path. 1 MiB is enough headroom for the
# preferences blob (typically a few KiB) while still refusing obvious
# DoS attempts before we even buffer the body in memory.
_MAX_URL_FETCH_BYTES = 1 * 1024 * 1024

# Connect+read timeout for ``import-url`` fetches. The spec calls for
# 5 seconds; we apply it as a single overall budget via ``httpx.Timeout``
# so a slow trickle can't keep the request open indefinitely.
_URL_FETCH_TIMEOUT_SECONDS = 5.0

# Allowed URL schemes for ``import-url``. ``file://``, ``ftp://`` and the
# like must never reach :func:`httpx.AsyncClient.get` — they could read
# local disk or hit internal services.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _actor(request: Request) -> str | None:
    """Best-effort client identity for the audit log.

    Mirrors the helper in :mod:`app.web.routes.settings_api` — Persona
    has no per-user session model, so the client IP is the closest
    stable identity we have. ``request.client`` can be ``None`` when a
    test client builds the request without a transport, so we fall back
    to ``None`` rather than raising.
    """
    return request.client.host if request.client is not None else None


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


@router.post("/settings/backup/import-url")
async def settings_backup_import_url(
    request: Request,
    url: Annotated[str, Form()],
) -> RedirectResponse:
    """Fetch a backup JSON from a remote URL and import it (merge mode).

    Useful for syncing preferences across machines: point each instance
    at the same blob URL (e.g. a private gist, an internal share) and
    re-trigger this endpoint after edits. The fetch is bounded by
    :data:`_URL_FETCH_TIMEOUT_SECONDS` and :data:`_MAX_URL_FETCH_BYTES`
    so a hostile target can't stall or OOM the importer.

    Only ``http(s)`` URLs are accepted — :data:`_ALLOWED_URL_SCHEMES`
    blocks ``file://`` / ``ftp://`` / friends which could otherwise be
    abused to read local disk or hit internal-only services.

    The merge flag is hard-wired to ``True`` (additive) because the URL
    flow is intended for "pull in updates from the canonical copy" use
    cases. Destructive replace must go through the explicit file-upload
    form, which already shows a warning banner.

    Every call — accepted or refused — is recorded in ``audit_log``
    under the ``settings_backup.import_url`` action.
    """
    actor = _actor(request)
    url_clean = url.strip()

    parsed = urlparse(url_clean)
    if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail=f"rejected: invalid scheme {parsed.scheme!r}",
            success=False,
        )
        log_import_url.warning(
            "rejected_scheme",
            actor=actor,
            scheme=parsed.scheme,
        )
        raise HTTPException(
            status_code=400,
            detail="url must use http or https scheme",
        )

    timeout = httpx.Timeout(_URL_FETCH_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url_clean)
    except (httpx.HTTPError, TimeoutError) as exc:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail=f"fetch failed: {type(exc).__name__}",
            success=False,
        )
        log_import_url.warning(
            "fetch_failed",
            actor=actor,
            error=type(exc).__name__,
        )
        raise HTTPException(
            status_code=400, detail=f"fetch failed: {exc}"
        ) from exc

    if response.status_code >= 400:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail=f"http {response.status_code}",
            success=False,
        )
        log_import_url.warning(
            "fetch_status",
            actor=actor,
            status=response.status_code,
        )
        raise HTTPException(
            status_code=400,
            detail=f"remote returned HTTP {response.status_code}",
        )

    raw = response.content
    if len(raw) == 0:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail="empty body",
            success=False,
        )
        raise HTTPException(status_code=400, detail="empty response body")
    if len(raw) > _MAX_URL_FETCH_BYTES:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail=f"too large: {len(raw)}B",
            success=False,
        )
        log_import_url.warning(
            "body_too_large",
            actor=actor,
            bytes=len(raw),
        )
        raise HTTPException(status_code=413, detail="remote body too large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail=f"invalid JSON: {type(exc).__name__}",
            success=False,
        )
        raise HTTPException(
            status_code=400, detail=f"invalid JSON: {exc}"
        ) from exc

    try:
        summary = await import_settings_json(payload, merge=True)
    except ValueError as exc:
        await log_action(
            action="settings_backup.import_url",
            actor=actor,
            target=url_clean[:200],
            detail=f"import rejected: {exc}",
            success=False,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    written = sum(summary.values())
    await log_action(
        action="settings_backup.import_url",
        actor=actor,
        target=url_clean[:200],
        detail=f"tables={len(summary)} rows={written} bytes={len(raw)}",
        success=True,
    )
    log_import_url.info(
        "ok",
        actor=actor,
        tables=len(summary),
        rows=written,
        bytes=len(raw),
        fetched_at=datetime.now(UTC).isoformat(),
    )
    return RedirectResponse(url="/settings/backup/manage", status_code=303)


__all__ = ["router"]
