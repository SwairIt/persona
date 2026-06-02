"""Create a cold-storage backup snapshot of the Persona database.

Produces `data/backups/persona-YYYYMMDD-HHMMSS.zip` containing:
  - persona.db (consistent copy via SQLite Online Backup API)
  - manifest.json with metadata
  - last 7 days of thumbnails (configurable via --days)

This is NOT encryption — that would need an external age binary. For now this
is a portable zip you can drop onto a USB stick or another machine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings

log = get_logger("persona.backup")


def main() -> int:
    parser = argparse.ArgumentParser(description="Persona cold-storage backup")
    parser.add_argument("--days", type=int, default=7, help="thumbnail history depth")
    parser.add_argument("--out-dir", type=Path, help="output directory (default data/backups)")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    out_dir = args.out_dir or (settings.data_dir / "backups")
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    zip_path = out_dir / f"persona-{timestamp}.zip"
    db_temp = out_dir / f"persona-{timestamp}.db"

    log.info("backup.start", out=str(zip_path), days=args.days)
    _copy_database(settings.db_path, db_temp)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(settings.db_path),
        "thumbnails_dir": str(settings.thumbnails_dir),
        "thumbnail_days": args.days,
        "persona_version": "0.1.0",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(db_temp, arcname="persona.db")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

        thumb_count = 0
        if settings.thumbnails_dir.exists():
            for thumb in settings.thumbnails_dir.rglob("*.webp"):
                try:
                    if thumb.stat().st_mtime >= cutoff.timestamp():
                        rel = thumb.relative_to(settings.thumbnails_dir)
                        zf.write(thumb, arcname=f"thumbnails/{rel.as_posix()}")
                        thumb_count += 1
                except OSError:
                    continue
        log.info("backup.thumbs", count=thumb_count)

    db_temp.unlink(missing_ok=True)
    size_mb = zip_path.stat().st_size / 1024 / 1024
    log.info("backup.done", path=str(zip_path), size_mb=round(size_mb, 2))
    return 0


def _copy_database(src: Path, dst: Path) -> None:
    """Use SQLite Online Backup API for a consistent copy (handles WAL)."""
    if not src.exists():
        shutil.copy2(src, dst) if src.exists() else dst.touch()
        return
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


if __name__ == "__main__":
    sys.exit(main())
