"""Legacy orphaned authentication sessions are removed on upgrade."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_migration_214_removes_only_orphaned_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "orphan-session.db"
    migration = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "storage"
        / "migrations"
        / "214_repair_orphan_auth_sessions.sql"
    ).read_text(encoding="utf-8")

    async with aiosqlite.connect(db_path, isolation_level=None) as db:
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        await db.execute(
            """
            CREATE TABLE auth_session(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        await db.execute("INSERT INTO users(id) VALUES(2)")
        await db.executemany(
            "INSERT INTO auth_session(id,user_id) VALUES(?,?)",
            ((10, 2), (11, 3)),
        )

        await db.executescript(migration)
        await db.execute("PRAGMA foreign_keys=ON")
        rows = await (
            await db.execute("SELECT id,user_id FROM auth_session ORDER BY id")
        ).fetchall()
        violations = await (await db.execute("PRAGMA foreign_key_check")).fetchall()

    assert rows == [(10, 2)]
    assert violations == []
