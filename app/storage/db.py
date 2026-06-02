"""Async SQLite connection management and schema bootstrap."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.settings import get_settings

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def init_database(db_path: Path | None = None) -> None:
    """Create the SQLite database, apply schema and all migrations. Idempotent."""
    target = db_path or get_settings().db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with aiosqlite.connect(target) as conn:
        await conn.executescript(schema_sql)
        if _MIGRATIONS_DIR.exists():
            for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                await conn.executescript(migration.read_text(encoding="utf-8"))
        await conn.commit()


@asynccontextmanager
async def get_connection(
    db_path: Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an async SQLite connection with sane pragmas."""
    target = db_path or get_settings().db_path
    async with aiosqlite.connect(target) as conn:
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = aiosqlite.Row
        yield conn
