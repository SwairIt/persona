"""Encrypted backup archives — passphrase → PBKDF2 → AES-256-GCM."""

from app.backup.archive import (
    BackupError,
    create_encrypted_backup,
    list_local_backups,
    restore_encrypted_backup,
)

__all__ = [
    "BackupError",
    "create_encrypted_backup",
    "list_local_backups",
    "restore_encrypted_backup",
]
