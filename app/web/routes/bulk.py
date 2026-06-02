"""Bulk operations — delete all screenshots for an app, etc."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.bulk")

router = APIRouter(prefix="/api/bulk", tags=["bulk"])


@router.post("/delete-by-app", response_class=JSONResponse)
async def delete_by_app(
    app_name: str = Form(..., alias="app"),
    confirm: str = Form(...),
) -> JSONResponse:
    """Delete every screenshot for a given app and remove their thumbnail files.

    The caller must pass `confirm=<app_name>` to acknowledge — defends against
    accidental clicks.
    """
    if confirm != app_name:
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match app name.")

    deleted_rows = 0
    deleted_files = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots WHERE app_name = ?",
            (app_name,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            thumb = row["thumbnail_path"]
            if thumb:
                try:
                    if Path(thumb).exists():
                        Path(thumb).unlink()
                        deleted_files += 1
                except OSError as exc:
                    log.warning("bulk.delete.thumb_failed", path=str(thumb), error=str(exc))

        cursor = await conn.execute(
            "DELETE FROM screenshots WHERE app_name = ?",
            (app_name,),
        )
        deleted_rows = cursor.rowcount or 0
        await conn.commit()

    log.info("bulk.delete.done", app=app_name, rows=deleted_rows, files=deleted_files)
    return JSONResponse(
        {"app": app_name, "deleted_rows": deleted_rows, "deleted_files": deleted_files}
    )


@router.post("/delete-by-range", response_class=JSONResponse)
async def delete_by_range(
    since: str = Form(...),
    until: str = Form(...),
    confirm: str = Form(...),
) -> JSONResponse:
    """Delete every screenshot whose captured_at falls into the range."""
    if confirm.lower() != "yes":
        raise HTTPException(status_code=400, detail="Confirmation phrase must be exactly 'yes'.")

    deleted_rows = 0
    deleted_files = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots WHERE captured_at >= ? AND captured_at < ?",
            (since, until),
        )
        rows = await cursor.fetchall()
        for row in rows:
            thumb = row["thumbnail_path"]
            if thumb:
                try:
                    if Path(thumb).exists():
                        Path(thumb).unlink()
                        deleted_files += 1
                except OSError as exc:
                    log.warning("bulk.delete.thumb_failed", path=str(thumb), error=str(exc))

        cursor = await conn.execute(
            "DELETE FROM screenshots WHERE captured_at >= ? AND captured_at < ?",
            (since, until),
        )
        deleted_rows = cursor.rowcount or 0
        await conn.commit()

    log.info("bulk.delete.range", since=since, until=until, rows=deleted_rows, files=deleted_files)
    return JSONResponse({"deleted_rows": deleted_rows, "deleted_files": deleted_files})
