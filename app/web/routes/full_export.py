"""Single-ZIP download of everything Persona has stored — for migration."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.settings import get_settings

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/full.zip")
async def export_full() -> StreamingResponse:
    """Stream a ZIP containing the DB snapshot + every thumbnail + manifest."""
    settings = get_settings()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_temp:
        db_temp_path = Path(db_temp.name)
    _backup_database(settings.db_path, db_temp_path)

    buffer = io.BytesIO()
    thumb_count = 0
    try:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_temp_path, arcname="persona.db")

            if settings.thumbnails_dir.exists():
                for thumb in settings.thumbnails_dir.rglob("*.webp"):
                    try:
                        rel = thumb.relative_to(settings.thumbnails_dir)
                    except ValueError:
                        continue
                    zf.write(thumb, arcname=f"thumbnails/{rel.as_posix()}")
                    thumb_count += 1

            manifest = {
                "schema": "persona-full-1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "persona_version": "0.5.0",
                "thumbnail_count": thumb_count,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    finally:
        db_temp_path.unlink(missing_ok=True)

    buffer.seek(0)
    filename = f"persona-full-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _backup_database(src: Path, dst: Path) -> None:
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
