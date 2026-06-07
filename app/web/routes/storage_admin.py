"""User-facing /storage dashboard — disk usage + cleanup."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage_management import (
    get_settings,
    list_cleanup_runs,
    run_cleanup,
    set_settings,
    usage_breakdown,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["storage"])
log = get_logger("persona.storage_admin")


@router.get("/storage", response_class=HTMLResponse, response_model=None)
async def storage_dashboard(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    settings = await get_settings()
    usage = await usage_breakdown(recent_days=14)
    runs = await list_cleanup_runs(limit=10)
    return templates.TemplateResponse(
        request,
        "storage_dashboard.html",
        {
            "title": "Хранилище",
            "active_nav": "",
            "settings": settings,
            "usage": usage,
            "runs": runs,
        },
    )


@router.post("/storage/settings", response_model=None)
async def save_storage_settings(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    retention_days: Annotated[str, Form()] = "",
    quota_mb: Annotated[str, Form()] = "",
) -> RedirectResponse:
    r_int: int | None = None
    q_int: int | None = None
    if retention_days.strip():
        try:
            r_int = int(retention_days.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="retention_days must be a number") from exc
    if quota_mb.strip():
        try:
            q_int = int(quota_mb.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="quota_mb must be a number") from exc
    await set_settings(retention_days=r_int, quota_mb=q_int)
    return RedirectResponse(url="/storage", status_code=303)


@router.post("/storage/cleanup", response_model=None)
async def trigger_cleanup(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    older_than_days: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Run one cleanup pass. ``older_than_days`` lets the user override
    the saved retention for a one-shot purge."""
    override: int | None = None
    if older_than_days.strip():
        try:
            override = int(older_than_days.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="older_than_days must be a number"
            ) from exc
    await run_cleanup(trigger_source="manual", override_retention_days=override)
    return RedirectResponse(url="/storage", status_code=303)


@router.get("/api/storage/usage.json", response_class=JSONResponse)
async def storage_usage_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    usage = await usage_breakdown(recent_days=14)
    return JSONResponse(
        {
            "total_shots": usage.total_shots,
            "total_bytes": usage.total_bytes,
            "oldest_captured_at": usage.oldest_captured_at,
            "newest_captured_at": usage.newest_captured_at,
            "by_day_recent": usage.by_day_recent,
        }
    )


# --- T16 (2026-06-07): bulk export ZIP for moving data between devices ---


@router.get("/storage/export.zip", response_model=None)
async def export_bundle(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    from_date: str = "",
    to_date: str = "",
    max_shots: int = 1000,
) -> Response:
    """Pack a date range of screenshots into a ZIP for offline transfer.

    Use case: user wants to move N days of data from one Persona
    installation to another (e.g. fresh server, archived disk). The ZIP
    contains:
        manifest.json   — array of {id, captured_at, app_name, ocr_text}
        shots/{id}.ext  — the original thumbnail bytes
    """
    import io  # noqa: PLC0415 - lazy imports keep cold-start cheap
    import json
    import os
    import zipfile

    from fastapi.responses import StreamingResponse

    from app.storage.db import get_connection

    max_shots = max(1, min(int(max_shots), 5000))

    # Build the date predicate. Both empty → entire history.
    clauses: list[str] = []
    params: list[object] = []
    if from_date.strip():
        clauses.append("captured_at >= ?")
        params.append(from_date.strip() + " 00:00:00")
    if to_date.strip():
        clauses.append("captured_at <= ?")
        params.append(to_date.strip() + " 23:59:59")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max_shots)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title, ocr_text, "
            "       thumbnail_path "
            f"FROM screenshots{where} "
            "ORDER BY captured_at ASC LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()

    buf = io.BytesIO()
    manifest: list[dict[str, object]] = []
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            shot_id = int(row["id"])
            path = str(row["thumbnail_path"] or "")
            manifest_entry = {
                "id": shot_id,
                "captured_at": str(row["captured_at"]),
                "app_name": (
                    str(row["app_name"]) if row["app_name"] is not None else None
                ),
                "window_title": (
                    str(row["window_title"])
                    if row["window_title"] is not None
                    else None
                ),
                "ocr_text": (
                    str(row["ocr_text"]) if row["ocr_text"] is not None else None
                ),
            }
            if path and os.path.exists(path):
                ext = os.path.splitext(path)[1].lstrip(".") or "webp"
                try:
                    zf.write(path, arcname=f"shots/{shot_id}.{ext}")
                    manifest_entry["file"] = f"shots/{shot_id}.{ext}"
                except OSError as exc:
                    manifest_entry["file_error"] = str(exc)
            manifest.append(manifest_entry)
        zf.writestr(
            "manifest.json",
            json.dumps(
                {"shots": manifest, "exported_at": str(get_settings_ts())},
                indent=2,
                ensure_ascii=False,
            ),
        )
    buf.seek(0)
    filename = f"persona-export-{(from_date or 'all')}-{(to_date or 'all')}.zip"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def get_settings_ts() -> str:
    """Tiny helper for ISO-now without importing datetime at module top."""
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).isoformat()
