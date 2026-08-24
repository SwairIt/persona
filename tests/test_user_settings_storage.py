"""Migration 228 + the per-user settings storage helpers.

``user_settings`` is the per-user twin of the global ``kv_settings`` table:
a registered non-owner user stores its own LLM provider/key/model and UI
prefs there, scoped by ``user_id`` and cascaded away with the user row.

The tests below prove the four things that would silently break the feature:
the migration really applied (table + the new ``llm_usage.user_id`` column),
the async round-trip upserts instead of duplicating, two users never see each
other's rows, and the synchronous Jinja-side reader keys its TTL cache by
``(user_id, key)`` so one user's theme can never be served to another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.storage.db import get_connection
from app.storage.repository import delete_user_kv, get_user_kv, set_user_kv
from app.web.templates_engine import (
    _user_kv_value_cache,
    get_user_kv_sync,
    invalidate_user_kv_sync,
)

if TYPE_CHECKING:
    import aiosqlite


@pytest.fixture(autouse=True)
def _clear_sync_cache() -> None:
    """The sync TTL cache is process-global; user ids repeat across temp DBs."""
    _user_kv_value_cache.clear()


async def _make_user(conn: aiosqlite.Connection, email: str) -> int:
    cursor = await conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, 'x')",
        (email,),
    )
    await conn.commit()
    user_id = cursor.lastrowid
    assert user_id is not None
    return int(user_id)


@pytest.mark.asyncio
async def test_migration_228_creates_user_settings(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings'"
    )
    assert await cursor.fetchone() is not None

    cursor = await db.execute("PRAGMA table_info(user_settings)")
    columns = {str(row["name"]): row for row in await cursor.fetchall()}
    assert set(columns) == {"user_id", "key", "value", "updated_at"}
    # Composite primary key (user_id, key) — the UPSERT target.
    assert {name for name, row in columns.items() if int(row["pk"])} == {"user_id", "key"}

    cursor = await db.execute("PRAGMA foreign_key_list(user_settings)")
    foreign_keys = [
        (str(row["table"]), str(row["from"]), str(row["to"]), str(row["on_delete"]))
        for row in await cursor.fetchall()
    ]
    assert foreign_keys == [("users", "user_id", "id", "CASCADE")]


@pytest.mark.asyncio
async def test_migration_228_adds_llm_usage_user_id(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(llm_usage)")
    columns = {str(row["name"]): row for row in await cursor.fetchall()}
    assert "user_id" in columns
    # Nullable on purpose: pre-228 rows have no attributable user.
    assert int(columns["user_id"]["notnull"]) == 0


@pytest.mark.asyncio
async def test_get_set_delete_roundtrip(db: aiosqlite.Connection) -> None:
    user_id = await _make_user(db, "roundtrip@example.com")

    assert await get_user_kv(db, user_id, "llm_provider") is None

    await set_user_kv(db, user_id, "llm_provider", "ollama")
    assert await get_user_kv(db, user_id, "llm_provider") == "ollama"

    await delete_user_kv(db, user_id, "llm_provider")
    assert await get_user_kv(db, user_id, "llm_provider") is None
    # Deleting an absent key is a no-op, not an error.
    await delete_user_kv(db, user_id, "llm_provider")


@pytest.mark.asyncio
async def test_set_twice_updates_instead_of_duplicating(db: aiosqlite.Connection) -> None:
    user_id = await _make_user(db, "upsert@example.com")

    await set_user_kv(db, user_id, "llm_model", "qwen2.5:3b")
    await set_user_kv(db, user_id, "llm_model", "qwen3:4b")

    assert await get_user_kv(db, user_id, "llm_model") == "qwen3:4b"
    cursor = await db.execute(
        "SELECT COUNT(*) AS n FROM user_settings WHERE user_id = ? AND key = ?",
        (user_id, "llm_model"),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert int(row["n"]) == 1


@pytest.mark.asyncio
async def test_values_are_isolated_between_users(db: aiosqlite.Connection) -> None:
    first = await _make_user(db, "first@example.com")
    second = await _make_user(db, "second@example.com")

    await set_user_kv(db, first, "theme", "dark")
    await set_user_kv(db, second, "theme", "cosmos")

    assert await get_user_kv(db, first, "theme") == "dark"
    assert await get_user_kv(db, second, "theme") == "cosmos"

    # Deleting one user's key leaves the other's row untouched.
    await delete_user_kv(db, first, "theme")
    assert await get_user_kv(db, first, "theme") is None
    assert await get_user_kv(db, second, "theme") == "cosmos"


@pytest.mark.asyncio
async def test_app_connections_enable_foreign_keys() -> None:
    """The cascade below is only real because get_connection turns FKs on.

    SQLite defaults ``foreign_keys`` to OFF per connection, so this is a
    load-bearing assertion rather than a tautology.
    """
    async with get_connection() as conn:
        cursor = await conn.execute("PRAGMA foreign_keys")
        row = await cursor.fetchone()
        assert row is not None
        assert int(row[0]) == 1


@pytest.mark.asyncio
async def test_settings_cascade_when_user_deleted(db: aiosqlite.Connection) -> None:
    user_id = await _make_user(db, "cascade@example.com")
    keeper = await _make_user(db, "keeper@example.com")

    async with get_connection() as conn:
        await set_user_kv(conn, user_id, "llm_api_key", "secret")
        await set_user_kv(conn, user_id, "theme", "light")
        await set_user_kv(conn, keeper, "theme", "dark")

        await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await conn.commit()

        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert int(row["n"]) == 0
        assert await get_user_kv(conn, keeper, "theme") == "dark"


@pytest.mark.asyncio
async def test_sync_helper_reads_what_async_wrote(db: aiosqlite.Connection) -> None:
    user_id = await _make_user(db, "sync@example.com")

    # ttl=0 bypasses the process TTL cache so each call re-reads the file.
    assert get_user_kv_sync(user_id, "theme", ttl=0.0) is None

    await set_user_kv(db, user_id, "theme", "cosmos-dark")
    assert get_user_kv_sync(user_id, "theme", ttl=0.0) == "cosmos-dark"

    await delete_user_kv(db, user_id, "theme")
    assert get_user_kv_sync(user_id, "theme", ttl=0.0) is None


@pytest.mark.asyncio
async def test_sync_cache_key_does_not_leak_between_users(db: aiosqlite.Connection) -> None:
    first = await _make_user(db, "cache-a@example.com")
    second = await _make_user(db, "cache-b@example.com")

    await set_user_kv(db, first, "theme", "dark")
    await set_user_kv(db, second, "theme", "light")

    # Default TTL: both values are served from the cache after the first read.
    # A cache keyed by ``key`` alone would return "dark" for both users here.
    assert get_user_kv_sync(first, "theme") == "dark"
    assert get_user_kv_sync(second, "theme") == "light"
    assert get_user_kv_sync(first, "theme") == "dark"

    # The cache is warm, so a fresh write is invisible until it is invalidated.
    await set_user_kv(db, first, "theme", "persona")
    assert get_user_kv_sync(first, "theme") == "dark"
    invalidate_user_kv_sync(first, "theme")
    assert get_user_kv_sync(first, "theme") == "persona"
    # Invalidating one user must not evict the other's entry.
    assert get_user_kv_sync(second, "theme") == "light"
