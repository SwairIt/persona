"""Private vault — encrypt screenshot OCR + thumbnail with a passphrase.

The encrypted blob is a small JSON envelope: {ocr, thumbnail_b64}, run
through the existing AES-256-GCM helpers from `app/backup/crypto.py` (so
we reuse the audited primitive, no homegrown crypto here).

Marking a screenshot as private:
  1. Read OCR + thumbnail bytes
  2. JSON-encode them
  3. Encrypt with passphrase
  4. Store encrypted blob in `private_vault` table
  5. Delete plaintext thumbnail file, NULL out OCR text, set is_private=1

Unlocking (read-only, in-memory) is the reverse, gated by the passphrase
the user re-enters at view time.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from app.backup.crypto import CryptoError, decrypt, encrypt, fingerprint
from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.vault")


class VaultError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UnlockedScreenshot:
    ocr_text: str | None
    thumbnail_bytes: bytes | None


async def make_private(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    passphrase: str,
) -> None:
    """Encrypt OCR + thumbnail into the vault, then strip plaintext from disk + DB."""
    if len(passphrase) < 8:
        msg = "Passphrase must be at least 8 characters."
        raise VaultError(msg)

    cursor = await conn.execute(
        "SELECT ocr_text, thumbnail_path, is_private FROM screenshots WHERE id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        msg = "Screenshot not found"
        raise VaultError(msg)
    if int(row["is_private"]) == 1:
        msg = "Already private"
        raise VaultError(msg)

    ocr_text = row["ocr_text"]
    thumb_path = row["thumbnail_path"]

    thumbnail_b64: str | None = None
    if thumb_path:
        thumbnail_path = Path(thumb_path)
        if thumbnail_path.exists():
            thumbnail_b64 = base64.b64encode(thumbnail_path.read_bytes()).decode("ascii")

    payload = json.dumps(
        {"ocr": ocr_text, "thumbnail_b64": thumbnail_b64},
        ensure_ascii=False,
    ).encode("utf-8")

    try:
        encrypted = encrypt(payload, passphrase)
    except CryptoError as exc:
        raise VaultError(str(exc)) from exc

    await conn.execute(
        """
        INSERT INTO private_vault (screenshot_id, encrypted_payload, fingerprint)
        VALUES (?, ?, ?)
        ON CONFLICT(screenshot_id) DO UPDATE SET
            encrypted_payload = excluded.encrypted_payload,
            fingerprint = excluded.fingerprint,
            created_at = datetime('now')
        """,
        (screenshot_id, encrypted, fingerprint(passphrase)),
    )
    await conn.execute(
        "UPDATE screenshots SET is_private = 1, ocr_text = NULL, ocr_status = 'skipped' "
        "WHERE id = ?",
        (screenshot_id,),
    )
    await conn.commit()

    if thumb_path:
        path = Path(thumb_path)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                log.warning("vault.unlink_failed", path=str(path), error=str(exc))

    await conn.execute(
        "UPDATE screenshots SET thumbnail_path = NULL WHERE id = ?",
        (screenshot_id,),
    )
    await conn.commit()


async def unlock(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    passphrase: str,
) -> UnlockedScreenshot:
    """Decrypt the vault entry in-memory. Does NOT restore plaintext to disk/DB."""
    cursor = await conn.execute(
        "SELECT encrypted_payload, fingerprint FROM private_vault WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        msg = "No vault entry for this screenshot"
        raise VaultError(msg)

    if str(row["fingerprint"]) != fingerprint(passphrase):
        msg = "Wrong passphrase"
        raise VaultError(msg)

    try:
        payload = decrypt(bytes(row["encrypted_payload"]), passphrase)
    except CryptoError as exc:
        raise VaultError(str(exc)) from exc

    obj: dict[str, Any] = json.loads(payload.decode("utf-8"))
    thumb_b64 = obj.get("thumbnail_b64")
    thumb_bytes: bytes | None = None
    if thumb_b64:
        thumb_bytes = base64.b64decode(thumb_b64)

    return UnlockedScreenshot(ocr_text=obj.get("ocr"), thumbnail_bytes=thumb_bytes)


async def restore_to_public(
    conn: aiosqlite.Connection,
    *,
    screenshot_id: int,
    passphrase: str,
) -> None:
    """Decrypt + write back plaintext to disk/DB, drop vault entry."""
    unlocked = await unlock(conn, screenshot_id=screenshot_id, passphrase=passphrase)

    settings = get_settings()
    thumb_path: Path | None = None
    if unlocked.thumbnail_bytes:
        captured_cursor = await conn.execute(
            "SELECT captured_at FROM screenshots WHERE id = ?",
            (screenshot_id,),
        )
        captured_row = await captured_cursor.fetchone()
        from app.storage.time import parse_iso

        captured_at = parse_iso(str(captured_row["captured_at"]))
        day_dir = settings.thumbnails_dir / captured_at.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = day_dir / f"{screenshot_id}.webp"
        thumb_path.write_bytes(unlocked.thumbnail_bytes)

    await conn.execute(
        "UPDATE screenshots SET is_private = 0, ocr_text = ?, "
        "ocr_status = CASE WHEN ? IS NULL THEN 'skipped' ELSE 'done' END, "
        "thumbnail_path = ? WHERE id = ?",
        (
            unlocked.ocr_text,
            unlocked.ocr_text,
            str(thumb_path) if thumb_path else None,
            screenshot_id,
        ),
    )
    await conn.execute(
        "DELETE FROM private_vault WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    await conn.commit()


async def count_private(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots WHERE is_private = 1"
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0
