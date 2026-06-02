"""Encrypted backup archive — combines `scripts/backup_snapshot.py` logic
with `crypto.py` to produce a single `.pbkx` file (Persona BacKup encrYpted).

Layout:

    1. Build an in-memory ZIP containing: persona.db, manifest.json,
       optional last-N-days thumbnails.
    2. Encrypt the entire ZIP with AES-256-GCM.
    3. Write the resulting envelope to disk.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backup.crypto import CryptoError, decrypt, encrypt, fingerprint
from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.backup")

DEFAULT_EXTENSION = ".pbkx"


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupSummary:
    path: Path
    bytes_written: int
    thumbnails_included: int
    fingerprint: str
    generated_at: datetime


def create_encrypted_backup(
    out_path: Path,
    *,
    passphrase: str,
    thumbnail_days: int = 7,
) -> BackupSummary:
    """Snapshot the DB + recent thumbnails into an encrypted single file."""
    if len(passphrase) < 12:
        msg = "Passphrase must be at least 12 characters."
        raise BackupError(msg)

    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=thumbnail_days)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db_temp:
        db_temp_path = Path(db_temp.name)
    try:
        _backup_database(settings.db_path, db_temp_path)

        manifest = {
            "schema": "persona-backup-1",
            "generated_at": now.isoformat(),
            "persona_version": "0.3.0",
            "thumbnail_days": thumbnail_days,
            "passphrase_fingerprint": fingerprint(passphrase),
        }

        zip_buffer = io.BytesIO()
        thumbnails_included = 0
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_temp_path, arcname="persona.db")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

            if settings.thumbnails_dir.exists():
                for thumb in settings.thumbnails_dir.rglob("*.webp"):
                    try:
                        if thumb.stat().st_mtime >= cutoff.timestamp():
                            rel = thumb.relative_to(settings.thumbnails_dir)
                            zf.write(thumb, arcname=f"thumbnails/{rel.as_posix()}")
                            thumbnails_included += 1
                    except OSError:
                        continue

        ciphertext = encrypt(zip_buffer.getvalue(), passphrase)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(ciphertext)
    finally:
        db_temp_path.unlink(missing_ok=True)

    summary = BackupSummary(
        path=out_path,
        bytes_written=out_path.stat().st_size,
        thumbnails_included=thumbnails_included,
        fingerprint=manifest["passphrase_fingerprint"],
        generated_at=now,
    )
    log.info(
        "backup.encrypted.done",
        out=str(out_path),
        bytes=summary.bytes_written,
        thumbs=thumbnails_included,
    )
    return summary


def restore_encrypted_backup(
    archive_path: Path,
    *,
    passphrase: str,
    restore_dir: Path,
) -> dict[str, object]:
    """Decrypt an archive and extract DB + thumbnails into `restore_dir`."""
    blob = archive_path.read_bytes()
    try:
        payload = decrypt(blob, passphrase)
    except CryptoError as exc:
        msg = str(exc)
        raise BackupError(msg) from exc

    restore_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    with zipfile.ZipFile(io.BytesIO(payload), "r") as zf:
        zf.extractall(restore_dir)
        extracted = len(zf.namelist())

    manifest_path = restore_dir / "manifest.json"
    manifest: dict[str, object] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    log.info("backup.restore.done", restore_dir=str(restore_dir), files=extracted)
    return {"files_extracted": extracted, "manifest": manifest, "restore_dir": str(restore_dir)}


def list_local_backups() -> list[dict[str, object]]:
    settings = get_settings()
    backups_dir = settings.data_dir / "backups"
    if not backups_dir.exists():
        return []
    items: list[dict[str, object]] = []
    for path in sorted(backups_dir.glob(f"*{DEFAULT_EXTENSION}")):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return items


def _backup_database(src: Path, dst: Path) -> None:
    """SQLite Online Backup API — handles WAL safely."""
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
