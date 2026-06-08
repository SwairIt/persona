"""T27 — /workspace page: browse + download files written by the AI."""

from __future__ import annotations

import datetime as _dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.web.templates_engine import templates
from app.workspace import (
    WorkspaceEscape,
    ensure_user_workspace,
    list_user_files,
    resolve_user_path,
)

router = APIRouter(tags=["workspace"])


@router.get("/workspace", response_class=HTMLResponse, response_model=None)
async def workspace_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    files = list_user_files(session["user_id"])
    root = ensure_user_workspace(session["user_id"])

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


def _format_size(size: int) -> str:
    """Human-readable bytes (KB/MB/GB)."""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"
