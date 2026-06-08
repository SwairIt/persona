"""T27 — /workspace page: browse + download files written by the AI.

T28 — adds the agent-facing ``GET /api/workspace/sync`` endpoint that the
user's chosen code-write-target device polls to mirror those files down.
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.devices import get_code_write_target, lookup_by_token
from app.logging_setup import get_logger
from app.web.templates_engine import templates
from app.workspace import (
    WorkspaceEscape,
    build_sync_payload,
    ensure_user_workspace,
    list_user_files,
    resolve_user_path,
)

router = APIRouter(tags=["workspace"])
log = get_logger("persona.workspace.routes")


@router.get("/workspace", response_class=HTMLResponse, response_model=None)
async def workspace_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    files = list_user_files(session["user_id"])
    root = ensure_user_workspace(session["user_id"])
    code_target = await get_code_write_target(session["user_id"])

    # Format for the template — readable size + ISO time.
    formatted = []
    for f in files:
        mtime = _dt.datetime.fromtimestamp(f["modified_at"]).strftime("%Y-%m-%d %H:%M")
        size_h = _format_size(f["size"]) if not f["is_dir"] else ""
        formatted.append({
            **f,
            "modified_at_human": mtime,
            "size_human": size_h,
        })

    return templates.TemplateResponse(
        request,
        "workspace_admin.html",
        {
            "title": "Workspace",
            "active_nav": "",
            "files": formatted,
            "workspace_path": str(root),
            "code_target": code_target,
            "user_email": session.get("email") if isinstance(session, dict) else "",
        },
    )


@router.get("/workspace/file/{path:path}", response_model=None)
async def workspace_download(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    path: str,
) -> FileResponse:
    """Download a file from the user's workspace."""
    try:
        p = resolve_user_path(session["user_id"], path)
    except WorkspaceEscape as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not p.exists() or p.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(
        path=p,
        filename=p.name,
        media_type="application/octet-stream",
    )


# --- Agent-facing API (T28) ------------------------------------------------


@router.get("/api/workspace/sync", response_class=JSONResponse)
async def workspace_sync(
    request: Request,
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> JSONResponse:
    """Pull workspace files for the device the user picked as code target.

    Auth is the ``X-Device-Token`` header (same pattern as
    ``/api/sync/*`` and ``/api/devices/heartbeat``). Only the device the
    user flagged via /devices is allowed through — every other device of
    the same user gets 403 so files don't fan out to the wrong machine.

    Query ``since`` is the device's last-seen event id (the device keeps
    its own watermark). The response carries ``cursor`` — the new
    watermark to persist — plus ``files`` (deduped to the latest op per
    path, write content read inline at pull time).
    """
    token = request.headers.get("x-device-token", "")
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Device-Token header")
    device = await lookup_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    if not device["is_code_write_target"]:
        raise HTTPException(
            status_code=403,
            detail="this device is not the code write target — pick it at /devices",
        )

    payload = await build_sync_payload(
        device["user_id"], since_id=since, limit=limit
    )
    return JSONResponse(
        {
            "device_id": device["id"],
            "cursor": payload["cursor"],
            "files": payload["files"],
            "count": len(payload["files"]),
        }
    )


def _format_size(size: int) -> str:
    """Human-readable bytes (KB/MB/GB)."""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"
