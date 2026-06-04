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
    """Stream a ZIP containing the DB snapshot + every thumbnail + manifest.

    v1.15 also includes audio_segments and remote-agent uploads
    (data/agent/) so the export is genuinely "everything I have".
    """
    settings = get_settings()

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_temp:
        db_temp_path = Path(db_temp.name)
    _backup_database(settings.db_path, db_temp_path)

    buffer = io.BytesIO()
    thumb_count = 0
    audio_count = 0
    agent_count = 0
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

            audio_dir = settings.data_dir / "audio_segments"
            if audio_dir.exists():
                for audio in audio_dir.rglob("*"):
                    if not audio.is_file():
                        continue
                    try:
                        rel = audio.relative_to(audio_dir)
                    except ValueError:
                        continue
                    zf.write(audio, arcname=f"audio_segments/{rel.as_posix()}")
                    audio_count += 1

            agent_dir = settings.data_dir / "agent"
            if agent_dir.exists():
                for f in agent_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    try:
                        rel = f.relative_to(agent_dir)
                    except ValueError:
                        continue
                    zf.write(f, arcname=f"agent/{rel.as_posix()}")
                    agent_count += 1

            from app import __version__ as persona_version  # noqa: PLC0415

            manifest = {
                "schema": "persona-full-2",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "persona_version": persona_version,
                "thumbnail_count": thumb_count,
                "audio_segment_files": audio_count,
                "agent_upload_files": agent_count,
                "files_in_root": ["persona.db", "manifest.json", "README.md"],
                "schema_doc": (
                    "persona.db is a standard SQLite3 file. Open it with "
                    "any SQLite tool. Schema lives in screenshots, audio_segment, "
                    "hourly_card, daily_pin, kv_settings, audit_log. "
                    "thumbnails/ holds WebP files by year/month/day. "
                    "audio_segments/ holds .opus / .wav files. "
                    "agent/<id>/ holds uploads from each remote-agent."
                ),
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            zf.writestr("README.md", _make_readme(manifest))
    finally:
        db_temp_path.unlink(missing_ok=True)

    buffer.seek(0)
    filename = f"persona-full-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _make_readme(manifest: dict[str, object]) -> str:
    """Human-readable README inside the export so the zip is self-describing."""
    return (
        "# Persona full export\n\n"
        f"Generated at: {manifest['generated_at']}\n"
        f"Persona version: {manifest['persona_version']}\n\n"
        "## Contents\n\n"
        "- `persona.db` — SQLite database with all metadata, notes, tags, "
        "audit log, hourly cards, daily pins.\n"
        f"- `thumbnails/` — {manifest['thumbnail_count']} WebP screen thumbnails "
        "(year/month/day folders).\n"
        f"- `audio_segments/` — {manifest['audio_segment_files']} speech-only "
        "Opus/WAV segments.\n"
        f"- `agent/<id>/` — {manifest['agent_upload_files']} files uploaded by "
        "remote agents (Mac etc).\n"
        "- `manifest.json` — schema metadata, counts, integrity hints.\n\n"
        "## Re-import\n\n"
        "Drop `persona.db` over your old `data/persona.db`, then copy "
        "`thumbnails/`, `audio_segments/`, `agent/` back into `data/`. "
        "Restart uvicorn.\n\n"
        "## Schema\n\n"
        f"{manifest['schema_doc']}\n"
    )


def _backup_database(src: Path, dst: Path) -> None:
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
