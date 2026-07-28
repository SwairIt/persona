"""Contract tests for the production migration ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiosqlite
import pytest

from app.storage import migration_runner
from app.storage.db import init_database
from app.storage.migration_runner import (
    MigrationChecksumError,
    MigrationFailedState,
    discover_migrations,
    migrate,
)


async def _run_small_migration(
    db_path: Path,
    migrations_dir: Path,
    *,
    schema_sql: str = "CREATE TABLE IF NOT EXISTS base_table(id INTEGER PRIMARY KEY);",
) -> None:
    async with aiosqlite.connect(db_path, isolation_level=None) as conn:
        await conn.execute("PRAGMA busy_timeout = 30000")
        await conn.execute("PRAGMA foreign_keys = ON")
        await migrate(
            conn,
            schema_sql=schema_sql,
            migrations_dir=migrations_dir,
            sqlite_vec_loaded=False,
        )


@pytest.mark.asyncio
async def test_fresh_install_records_manifest_and_second_start_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "fresh.db"
    await init_database(db_path)

    expected = discover_migrations(
        Path(migration_runner.__file__).parent / "migrations"
    )
    async with aiosqlite.connect(db_path) as conn:
        row = await (
            await conn.execute(
                """
                SELECT COUNT(*), MIN(status), MAX(status), SUM(is_baseline)
                FROM schema_migration
                """
            )
        ).fetchone()
        assert row == (len(expected), "applied", "applied", 0)
        metadata = await (
            await conn.execute(
                """
                SELECT COUNT(*)
                FROM schema_migration
                WHERE LENGTH(checksum) = 64
                  AND started_at IS NOT NULL
                  AND finished_at IS NOT NULL
                  AND applied_at IS NOT NULL
                  AND duration_ms >= 0
                """
            )
        ).fetchone()
        assert metadata == (len(expected),)

    async def unexpected_body(*args: object, **kwargs: object) -> list[tuple[str, str]]:
        raise AssertionError("second startup must execute zero migration bodies")

    monkeypatch.setattr(migration_runner, "_execute_script", unexpected_body)
    await init_database(db_path)


@pytest.mark.asyncio
async def test_current_legacy_database_is_baselined_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "legacy-current.db"
    await init_database(db_path)
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "UPDATE mcp_server SET description = 'operator custom value' "
            "WHERE name = 'filesystem'"
        )
        await conn.execute("DROP TABLE schema_migration")
        await conn.execute("DROP TABLE schema_capability")
        await conn.commit()

    async def unexpected_body(*args: object, **kwargs: object) -> list[tuple[str, str]]:
        raise AssertionError("reviewed legacy head must be baselined, not replayed")

    monkeypatch.setattr(migration_runner, "_execute_script", unexpected_body)
    await init_database(db_path)

    async with aiosqlite.connect(db_path) as conn:
        description = await (
            await conn.execute(
                "SELECT description FROM mcp_server WHERE name = 'filesystem'"
            )
        ).fetchone()
        baseline = await (
            await conn.execute(
                "SELECT COUNT(*), SUM(is_baseline) FROM schema_migration"
            )
        ).fetchone()
    assert description == ("operator custom value",)
    assert baseline[0] == baseline[1]
    assert baseline[0] > 190


@pytest.mark.asyncio
async def test_repaired_legacy_database_with_empty_ledger_is_baselined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery must not replay history merely because ledger tables remain."""
    db_path = tmp_path / "legacy-repaired.db"
    await init_database(db_path)
    async with aiosqlite.connect(db_path, isolation_level=None) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("DELETE FROM schema_migration")
        await conn.execute("DELETE FROM schema_capability")
        await conn.execute("COMMIT")

    async def unexpected_body(*args: object, **kwargs: object) -> list[tuple[str, str]]:
        raise AssertionError("empty repaired legacy ledger must be baselined")

    monkeypatch.setattr(migration_runner, "_execute_script", unexpected_body)
    await init_database(db_path)

    async with aiosqlite.connect(db_path) as conn:
        baseline = await (
            await conn.execute(
                "SELECT COUNT(*), SUM(is_baseline), MIN(status), MAX(status) "
                "FROM schema_migration"
            )
        ).fetchone()
    assert baseline[0] == baseline[1]
    assert baseline[2:] == ("applied", "applied")


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_and_is_not_marked_applied(
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_broken.sql").write_text(
        "CREATE TABLE should_rollback(id INTEGER);"
        "INSERT INTO table_that_does_not_exist VALUES (1);",
        encoding="utf-8",
    )
    db_path = tmp_path / "failed.db"

    with pytest.raises(aiosqlite.OperationalError, match="no such table"):
        await _run_small_migration(db_path, migrations_dir)

    async with aiosqlite.connect(db_path) as conn:
        failed = await (
            await conn.execute(
                """
                SELECT status, applied_at, error
                FROM schema_migration WHERE name = '001_broken.sql'
                """
            )
        ).fetchone()
        table = await (
            await conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='should_rollback'"
            )
        ).fetchone()
    assert failed is not None
    assert failed[0] == "failed"
    assert failed[1] is None
    assert "no such table" in failed[2]
    assert table is None

    with pytest.raises(MigrationFailedState, match="previously failed"):
        await _run_small_migration(db_path, migrations_dir)


@pytest.mark.asyncio
async def test_checksum_drift_blocks_startup(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    migration = migrations_dir / "001_example.sql"
    migration.write_text(
        "CREATE TABLE example(id INTEGER PRIMARY KEY);",
        encoding="utf-8",
    )
    db_path = tmp_path / "checksum.db"
    await _run_small_migration(db_path, migrations_dir)

    migration.write_text(
        "CREATE TABLE example(id INTEGER PRIMARY KEY, changed TEXT);",
        encoding="utf-8",
    )
    with pytest.raises(MigrationChecksumError, match="Checksum mismatch"):
        await _run_small_migration(db_path, migrations_dir)


@pytest.mark.asyncio
async def test_concurrent_fresh_start_elects_one_migrator(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    results = await asyncio.gather(
        init_database(db_path),
        init_database(db_path),
        return_exceptions=True,
    )
    assert results == [None, None]

    expected_count = len(
        discover_migrations(Path(migration_runner.__file__).parent / "migrations")
    )
    async with aiosqlite.connect(db_path) as conn:
        row = await (
            await conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT migration_order), "
                "COUNT(DISTINCT name) FROM schema_migration"
            )
        ).fetchone()
    assert row == (expected_count, expected_count, expected_count)


def test_sql_splitter_handles_triggers_and_semicolons_in_literals() -> None:
    sql = """
    CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT);
    CREATE TRIGGER sample_ai AFTER INSERT ON sample
    BEGIN
        INSERT INTO sample(value) VALUES ('a;b');
        UPDATE sample SET value = 'done;still-one-literal' WHERE id = new.id;
    END;
    """
    statements = migration_runner.split_sql_statements(sql)
    assert len(statements) == 2
    assert "CREATE TRIGGER" in statements[1]
