"""Create a passphrase-encrypted backup archive (.pbkx).

Usage:
    PERSONA_BACKUP_PASSPHRASE='at least twelve chars' \\
        uv run python scripts/encrypted_backup.py --out data/backups/persona.pbkx

Restore:
    PERSONA_BACKUP_PASSPHRASE='...' \\
        uv run python scripts/encrypted_backup.py --restore data/backups/persona.pbkx --restore-dir restored/
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.backup import (
    BackupError,
    create_encrypted_backup,
    list_local_backups,
    restore_encrypted_backup,
)
from app.logging_setup import configure_logging, get_logger
from app.settings import get_settings

log = get_logger("persona.encrypted_backup")


def main() -> int:
    parser = argparse.ArgumentParser(description="Persona encrypted backup")
    parser.add_argument("--out", type=Path, help="output .pbkx file")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="how many recent days of thumbnails to include",
    )
    parser.add_argument("--restore", type=Path, help="restore from this .pbkx file")
    parser.add_argument(
        "--restore-dir",
        type=Path,
        help="directory to extract a restored backup into",
    )
    parser.add_argument("--list", action="store_true", help="list local backups and exit")
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    if args.list:
        for item in list_local_backups():
            print(
                f"{item['name']:40s} {item['size_bytes']:>10d}B  {item['modified_at']}"
            )
        return 0

    passphrase = os.environ.get("PERSONA_BACKUP_PASSPHRASE", "").strip()
    if not passphrase:
        print(
            "PERSONA_BACKUP_PASSPHRASE not set. Refusing to proceed.",
            file=sys.stderr,
        )
        return 1

    if args.restore:
        if not args.restore.exists():
            print(f"Archive not found: {args.restore}", file=sys.stderr)
            return 1
        target = args.restore_dir or settings.data_dir / "restored"
        try:
            info = restore_encrypted_backup(
                args.restore,
                passphrase=passphrase,
                restore_dir=target,
            )
        except BackupError as exc:
            print(f"Restore failed: {exc}", file=sys.stderr)
            return 1
        log.info("restore.summary", info=info)
        print(f"Restored {info['files_extracted']} files into {info['restore_dir']}")
        return 0

    out_path = args.out
    if out_path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = settings.data_dir / "backups" / f"persona-{ts}.pbkx"

    try:
        summary = create_encrypted_backup(
            out_path,
            passphrase=passphrase,
            thumbnail_days=args.days,
        )
    except BackupError as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"Wrote {summary.bytes_written:,} bytes to {summary.path} "
        f"(thumbnails: {summary.thumbnails_included}, fingerprint: {summary.fingerprint})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
