"""T27 — /workspace page: browse + download files written by the AI.

T28 — adds the agent-facing ``GET /api/workspace/sync`` endpoint that the
user's chosen code-write-target device polls to mirror those files down.
"""

from __future__ import annotations

import datetime as _dt
from typing import Annotated

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
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


@router.post("/api/workspace/push", response_class=JSONResponse)
async def workspace_push(
    request: Request,
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T29 — UPSTREAM sync: the code-target device uploads a file it changed
    locally INTO the server workspace, so the AI can read/edit the user's
    real code (not just files the AI itself wrote). Mirror of /sync (down).

    Auth: X-Device-Token, must be the code-write-target. Body:
    ``{relative_path, content}`` (UTF-8 text). Writes into the user's
    server workspace and records a workspace_file_event so the change is
    visible to the AI and re-syncs to other devices.
    """
    from app.workspace import (  # noqa: PLC0415
        WorkspaceEscape,
        ensure_user_workspace,
        record_file_event,
        resolve_user_path,
    )

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

    rel = str(body.get("relative_path", "")).strip()
    content = body.get("content")
    if not rel or content is None:
        raise HTTPException(status_code=400, detail="relative_path and content required")
    content = str(content)
    if len(content.encode("utf-8")) > 2_000_000:
        raise HTTPException(status_code=413, detail="file too large (max 2 MB)")

    try:
        p = resolve_user_path(device["user_id"], rel)
    except WorkspaceEscape as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"write failed: {exc}") from exc

    rel_norm = p.relative_to(ensure_user_workspace(device["user_id"])).as_posix()
    await record_file_event(
        device["user_id"], rel_norm, "write", len(content.encode("utf-8"))
    )
    return JSONResponse({"ok": True, "relative_path": rel_norm})


@router.get("/api/agent/fs/pending", response_class=JSONResponse)
async def agent_fs_pending(request: Request) -> JSONResponse:
    """T29 — the Mac agent polls this for filesystem commands the AI queued
    (read/list/write). Returns the commands + the allowlist roots the agent
    must enforce. X-Device-Token auth, code-target only."""
    from app.devices.fs_rpc import get_pending, get_roots  # noqa: PLC0415

    token = request.headers.get("x-device-token", "")
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Device-Token header")
    device = await lookup_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    if not device["is_code_write_target"]:
        raise HTTPException(status_code=403, detail="this device is not the code target")
    return JSONResponse(
        {
            "commands": await get_pending(int(device["id"])),
            "roots": await get_roots(),
        }
    )


@router.post("/api/agent/fs/result", response_class=JSONResponse)
async def agent_fs_result(
    request: Request,
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """T29 — the agent posts the result of a filesystem command here."""
    from app.devices.fs_rpc import submit_result  # noqa: PLC0415

    token = request.headers.get("x-device-token", "")
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Device-Token header")
    device = await lookup_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    cmd_id = int(body.get("command_id") or 0)
    if cmd_id:
        await submit_result(
            cmd_id,
            str(body.get("status") or "error"),
            str(body.get("result") or ""),
        )
    return JSONResponse({"ok": True})


def _format_size(size: int) -> str:
    """Human-readable bytes (KB/MB/GB)."""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"
