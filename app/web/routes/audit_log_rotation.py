"""Operator UI + JSON endpoints for the audit-log rotation feature (v1.48).

Surfaces:

* ``GET  /admin/audit-log-rotation``                — HTML dashboard:
    live count of ``audit_log`` rows, last 10 archive runs, "Run Now"
    button, settings form (hour + enabled + keep_rows).
* ``POST /api/audit-log-rotation/run-now``          — Fire one off-cycle
    rotation. Returns the same dict shape as
    :func:`app.audit_log_rotation.rotate_audit_log`.
* ``GET  /api/audit-log-rotation/archives.json``    — List archive files
    on disk under the configured archive directory (one entry per file
    with name + bytes + mtime), so the UI can verify the disk state
    matches the bookkeeping table.
* ``POST /admin/audit-log-rotation/settings``       — Persist the hour
    / enabled / keep_rows kv rows from the settings form and PRG back.

All SQL is parametrised. Mutation endpoints return JSON so the page can
call them via ``fetch`` and re-render without a full reload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.audit_log_rotation import rotate_audit_log
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["audit-log-rotation"])

log = get_logger("persona.web.audit_log_rotation")

# kv rows shared with :mod:`app.workers.audit_log_rotation_worker`.
# Editing any of these names here means editing the worker too.
_KV_HOUR: str = "audit_log_rotation_hour_local"
_KV_ENABLED: str = "audit_log_rotation_enabled"
_KV_KEEP_ROWS: str = "audit_log_rotation_keep_rows"

_DEFAULT_HOUR: int = 4
_DEFAULT_KEEP_ROWS: int = 5000

#: Sanity cap on the keep-rows form input. A million rows is well past
#: anything a single-user install would ever accumulate, and lets us
#: catch fat-finger entries like an extra zero.
_KEEP_ROWS_MAX: int = 1_000_000

#: How many recent runs the dashboard lists.
_RECENT_RUNS_LIMIT: int = 10


@router.get("/admin/audit-log-rotation", response_class=HTMLResponse)
async def audit_log_rotation_page(request: Request) -> HTMLResponse:
    """Render the operator dashboard for audit-log rotation."""
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM audit_log")
        row = await cursor.fetchone()
        current_count = int(row["n"]) if row else 0

        cursor = await conn.execute(
            "SELECT id, archived_at, oldest_row_at, newest_row_at, "
            "rows_archived, file_path, file_size_bytes "
            "FROM audit_log_archive_run "
            "ORDER BY id DESC LIMIT ?",
            (_RECENT_RUNS_LIMIT,),
        )
        run_rows = await cursor.fetchall()

        raw_hour = await get_kv(conn, _KV_HOUR)
        raw_enabled = await get_kv(conn, _KV_ENABLED)
        raw_keep = await get_kv(conn, _KV_KEEP_ROWS)

    hour = _coerce_hour(raw_hour)
    # Worker defaults enabled=True when the kv row is missing — match
    # that default in the UI so a fresh install renders "on".
    enabled = (raw_enabled if raw_enabled is not None else "1").strip() == "1"
    keep_rows = _coerce_keep_rows(raw_keep)

    archive_dir = (get_settings().data_dir / "audit-archives").resolve()
    runs = [_run_row_to_dict(r) for r in run_rows]

    log.info(
        "audit_log_rotation.page",
        current_count=current_count,
        recent_runs=len(runs),
    )

    return templates.TemplateResponse(
        request,
        "audit_log_rotation.html",
        {
            "title": "Аудит-лог ротация",
            "active_nav": "settings",
            "current_count": current_count,
            "runs": runs,
            "hour": hour,
            "enabled": enabled,
            "keep_rows": keep_rows,
            "archive_dir": str(archive_dir),
        },
    )


@router.post("/api/audit-log-rotation/run-now")
async def audit_log_rotation_run_now() -> JSONResponse:
    """Trigger one off-cycle rotation. Returns the rotator's result dict.

    Useful for the operator who just bumped ``keep_rows`` down and
    wants the trim to happen now rather than wait until 04:00.
    """
    log.info("audit_log_rotation.run_now.start")
    keep_rows = await _read_keep_rows()
    result = await rotate_audit_log(keep_rows=keep_rows)
    log.info(
        "audit_log_rotation.run_now.done",
        status=result.get("status"),
        rows_archived=result.get("rows_archived", 0),
    )
    return JSONResponse({"ok": True, "result": dict(result)})


@router.get("/api/audit-log-rotation/archives.json")
async def audit_log_rotation_archives_json() -> JSONResponse:
    """List the gzipped JSONL archive files currently on disk.

    We list the filesystem (not the bookkeeping table) so the operator
    can spot drift — e.g. an archive file that was deleted off disk
    while the table still has a row for it, or vice versa. Sorted
    newest-first by mtime.
    """
    archive_dir = (get_settings().data_dir / "audit-archives").resolve()
    items: list[dict[str, Any]] = []
    if archive_dir.exists():
        for entry in archive_dir.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.endswith(".jsonl.gz"):
                continue
            try:
                stat = entry.stat()
            except OSError as exc:
                log.warning(
                    "audit_log_rotation.archive.stat_failed",
                    path=str(entry),
                    error=str(exc),
                )
                continue
            items.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "size_bytes": int(stat.st_size),
                    "mtime": float(stat.st_mtime),
                }
            )
    items.sort(key=lambda item: float(item["mtime"]), reverse=True)
    return JSONResponse(
        {"ok": True, "archive_dir": str(archive_dir), "items": items},
    )


@router.post("/admin/audit-log-rotation/settings")
async def audit_log_rotation_settings_save(
    hour: int = Form(...),
    keep_rows: int = Form(...),
    enabled: str = Form("off"),
) -> RedirectResponse:
    """Persist the hour / keep_rows / enabled kv rows, then PRG back."""
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=400, detail="hour must be 0..23")
    if not 0 <= keep_rows <= _KEEP_ROWS_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"keep_rows must be 0..{_KEEP_ROWS_MAX}",
        )
    is_on = enabled.strip().lower() in {"on", "1", "true", "yes"}
    async with get_connection() as conn:
        await set_kv(conn, _KV_HOUR, str(hour))
        await set_kv(conn, _KV_KEEP_ROWS, str(keep_rows))
        await set_kv(conn, _KV_ENABLED, "1" if is_on else "0")
    log.info(
        "audit_log_rotation.settings.saved",
        hour=hour,
        keep_rows=keep_rows,
        enabled=is_on,
    )
    return RedirectResponse(
        url="/admin/audit-log-rotation", status_code=303,
    )


async def _read_keep_rows() -> int:
    """Read the configured keep-rows value (kv); fall back to default."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_KEEP_ROWS)
    return _coerce_keep_rows(raw)


def _coerce_hour(raw: str | None) -> int:
    """Parse the stored hour kv row; fall back to the default."""
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_HOUR
    return value if 0 <= value <= 23 else _DEFAULT_HOUR


def _coerce_keep_rows(raw: str | None) -> int:
    """Parse the stored keep-rows kv row; fall back to the default."""
    if raw is None:
        return _DEFAULT_KEEP_ROWS
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return _DEFAULT_KEEP_ROWS
    if value < 0 or value > _KEEP_ROWS_MAX:
        return _DEFAULT_KEEP_ROWS
    return value


def _run_row_to_dict(row: Any) -> dict[str, Any]:
    """Convert an aiosqlite Row of ``audit_log_archive_run`` to a dict."""
    file_path = (
        str(row["file_path"]) if row["file_path"] is not None else ""
    )
    return {
        "id": int(row["id"]),
        "archived_at": str(row["archived_at"]),
        "oldest_row_at": (
            str(row["oldest_row_at"])
            if row["oldest_row_at"] is not None
            else None
        ),
        "newest_row_at": (
            str(row["newest_row_at"])
            if row["newest_row_at"] is not None
            else None
        ),
        "rows_archived": int(row["rows_archived"]),
        "file_path": file_path,
        "file_name": Path(file_path).name if file_path else "",
        "file_size_bytes": int(row["file_size_bytes"]),
    }


__all__ = ["router"]
