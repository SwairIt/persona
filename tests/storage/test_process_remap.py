"""Tests for process_app_remap CRUD + lookup."""

from __future__ import annotations

import aiosqlite
import pytest

from app.storage.process_remap import (
    delete_remap,
    list_remaps,
    lookup_remap,
    upsert_remap,
)


@pytest.mark.asyncio
async def test_upsert_and_lookup(db: aiosqlite.Connection) -> None:
    await upsert_remap(db, process_name="custom.exe", app_name="My Cool App")
    assert await lookup_remap(db, "custom.exe") == "My Cool App"
    assert await lookup_remap(db, "CUSTOM.EXE") == "My Cool App"


@pytest.mark.asyncio
async def test_upsert_overrides(db: aiosqlite.Connection) -> None:
    await upsert_remap(db, process_name="x.exe", app_name="First Name")
    await upsert_remap(db, process_name="X.EXE", app_name="Second Name")
    assert await lookup_remap(db, "x.exe") == "Second Name"


@pytest.mark.asyncio
async def test_list_and_delete(db: aiosqlite.Connection) -> None:
    await upsert_remap(db, process_name="a.exe", app_name="A")
    await upsert_remap(db, process_name="b.exe", app_name="B")
    items = await list_remaps(db)
    assert {item["process_name"] for item in items} == {"a.exe", "b.exe"}
    await delete_remap(db, "a.exe")
    assert await lookup_remap(db, "a.exe") is None


@pytest.mark.asyncio
async def test_rejects_empty(db: aiosqlite.Connection) -> None:
    with pytest.raises(ValueError):
        await upsert_remap(db, process_name="", app_name="X")
    with pytest.raises(ValueError):
        await upsert_remap(db, process_name="x.exe", app_name="  ")
