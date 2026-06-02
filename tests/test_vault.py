"""Tests for the private vault — encryption / unlock / restore lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

cryptography = pytest.importorskip("cryptography")

from app.storage.repository import get_screenshot, insert_screenshot, update_screenshot_ocr
from app.storage.vault import VaultError, count_private, make_private, restore_to_public, unlock


@pytest.mark.asyncio
async def test_make_private_strips_plaintext(db: aiosqlite.Connection, tmp_path) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="vault000000000001",
        app_name="Banking",
        window_title="Account",
    )
    await update_screenshot_ocr(db, sid, ocr_text="balance 12345.67 IBAN DE89", ocr_status="done")

    await make_private(db, screenshot_id=sid, passphrase="strong-passphrase-12")

    shot = await get_screenshot(db, sid)
    assert shot is not None
    assert shot.is_private is True
    assert shot.ocr_text is None
    assert await count_private(db) == 1


@pytest.mark.asyncio
async def test_unlock_returns_plaintext(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="vault000000000002",
        app_name="Bank",
    )
    await update_screenshot_ocr(db, sid, ocr_text="secret note", ocr_status="done")
    await make_private(db, screenshot_id=sid, passphrase="another-strong-pass")

    unlocked = await unlock(db, screenshot_id=sid, passphrase="another-strong-pass")
    assert unlocked.ocr_text == "secret note"


@pytest.mark.asyncio
async def test_unlock_wrong_passphrase(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="vault000000000003",
    )
    await update_screenshot_ocr(db, sid, ocr_text="hi", ocr_status="done")
    await make_private(db, screenshot_id=sid, passphrase="correct-passphrase-1")

    with pytest.raises(VaultError):
        await unlock(db, screenshot_id=sid, passphrase="wrong-passphrase-9")


@pytest.mark.asyncio
async def test_restore_to_public(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="vault000000000004",
    )
    await update_screenshot_ocr(db, sid, ocr_text="restorable", ocr_status="done")
    await make_private(db, screenshot_id=sid, passphrase="restore-pass-12345")

    await restore_to_public(db, screenshot_id=sid, passphrase="restore-pass-12345")
    shot = await get_screenshot(db, sid)
    assert shot is not None
    assert shot.is_private is False
    assert shot.ocr_text == "restorable"
    assert await count_private(db) == 0


@pytest.mark.asyncio
async def test_short_passphrase_rejected(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="vault000000000005",
    )
    with pytest.raises(VaultError):
        await make_private(db, screenshot_id=sid, passphrase="short")
