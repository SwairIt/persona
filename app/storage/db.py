"""Async SQLite connection management and schema bootstrap."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from app.logging_setup import get_logger
from app.observability.runtime import record_db_write_wait
from app.settings import get_settings
from app.storage.migration_runner import migrate

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

log = get_logger("persona.storage.db")

# ── sqlite-vec (векторный KNN) — ОПЦИОНАЛЬНО, с тихим fallback ────────────────
# Расширения SQLite — per-connection. Пробу пакета делаем один раз (кэш), саму
# загрузку — на каждом соединении. Если пакета/бинарника нет — векторный путь
# тихо отключается, FTS5/LIKE recall продолжают работать как раньше.
_VEC_PATH: str | None = None
_VEC_PROBED: bool = False
_VEC_USABLE: bool | None = None


def sqlite_vec_available() -> bool:
    """True, если sqlite-vec хоть раз успешно загрузилось (дешёвый гейт)."""
    return _VEC_USABLE is True


def _vec_loadable_path() -> str | None:
    global _VEC_PATH, _VEC_PROBED
    if _VEC_PROBED:
        return _VEC_PATH
    _VEC_PROBED = True
    try:
        import sqlite_vec  # noqa: PLC0415 — опциональная зависимость

        _VEC_PATH = sqlite_vec.loadable_path()
    except Exception as exc:  # noqa: BLE001 — пакет не установлен / нет бинарника
        log.info("sqlite_vec.unavailable", reason=str(exc))
        _VEC_PATH = None
    return _VEC_PATH


async def _load_sqlite_vec(conn: aiosqlite.Connection) -> bool:
    """Загрузить sqlite-vec на соединение. Любая ошибка → False, соединение цело."""
    global _VEC_USABLE
    path = _vec_loadable_path()
    if not path:
        _VEC_USABLE = False
        return False
    try:
        await conn.enable_load_extension(True)
        await conn.load_extension(path)
        await conn.enable_load_extension(False)
        _VEC_USABLE = True
        return True
    except Exception as exc:  # noqa: BLE001 — сборка без load_extension / ABI
        if _VEC_USABLE is None:
            log.info("sqlite_vec.load_failed", reason=str(exc))
        _VEC_USABLE = False
        try:
            await conn.enable_load_extension(False)
        except Exception:  # noqa: BLE001, S110
            pass
        return False


async def init_database(db_path: Path | None = None) -> None:
    """Create/upgrade SQLite once using the checksum-verified migration ledger."""
    target = db_path or get_settings().db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with aiosqlite.connect(target, isolation_level=None) as conn:
        # Configure locking before BEGIN IMMEDIATE.  The 30-second startup wait
        # lets a second web process wait for the elected migrator instead of
        # racing it or serving a partially upgraded schema.
        await conn.execute("PRAGMA busy_timeout = 30000")
        # journal_mode does not consistently honour busy_timeout while another
        # process is creating/migrating the same brand-new file.  Retry this
        # tiny bootstrap operation; BEGIN IMMEDIATE below remains the actual
        # migration election/lock.
        for attempt in range(60):
            try:
                await conn.execute("PRAGMA journal_mode = WAL")
                break
            except aiosqlite.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 59:
                    raise
                await asyncio.sleep(0.05)
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        vec_loaded = await _load_sqlite_vec(conn)
        await migrate(
            conn,
            schema_sql=schema_sql,
            migrations_dir=_MIGRATIONS_DIR,
            sqlite_vec_loaded=vec_loaded,
        )


async def _configure_connection(conn: aiosqlite.Connection) -> None:
    """Применить прагмы + sqlite-vec + row_factory к свежему соединению.

    Общий код для get_connection и write_transaction, чтобы настройки не
    разъезжались между чтением и записью.
    """
    await conn.execute("PRAGMA journal_mode = WAL")
    await conn.execute("PRAGMA synchronous = NORMAL")
    await conn.execute("PRAGMA foreign_keys = ON")
    # T19 fix (2026-06-07) — without busy_timeout the second writer
    # gets ``SQLITE_BUSY`` immediately and dies. With 40 background
    # workers all heartbeating every 3 sec, plus a slow Ollama call
    # holding a write transaction, lock contention spikes — the user
    # reports the site freezes. 5000ms gives writers time to queue
    # politely instead of throwing.
    await conn.execute("PRAGMA busy_timeout = 5000")
    # Read-speed pragmas (cheap, per-connection) — help FTS5 bm25 sorts,
    # vector KNN temp sorts, and large scans. mmap 256MB, 64MB page cache,
    # temp tables/indexes in RAM. Best-effort: a build that rejects a
    # pragma must never break the connection.
    try:
        await conn.execute("PRAGMA mmap_size = 268435456")
        await conn.execute("PRAGMA cache_size = -65536")
        await conn.execute("PRAGMA temp_store = MEMORY")
    except Exception:  # noqa: BLE001, S110
        pass
    # sqlite-vec per-connection (best-effort). Если расширения нет —
    # _VEC_USABLE станет False и больше не пробуем; векторный путь тихо
    # отключён, FTS/LIKE recall работают.
    if _VEC_USABLE is not False:
        await _load_sqlite_vec(conn)
    conn.row_factory = aiosqlite.Row


@asynccontextmanager
async def get_connection(
    db_path: Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Yield an async SQLite connection with sane pragmas."""
    target = db_path or get_settings().db_path
    async with aiosqlite.connect(target) as conn:
        await _configure_connection(conn)
        yield conn


@asynccontextmanager
async def write_transaction(
    db_path: Path | None = None,
) -> AsyncIterator[aiosqlite.Connection]:
    """Соединение в ЯВНОЙ ``BEGIN IMMEDIATE``-транзакции для записи.

    ``BEGIN IMMEDIATE`` сразу берёт RESERVED write-lock — это устраняет класс
    дедлоков при апгрейде read→write под конкуренцией (см. T19: 40 воркеров +
    медленный Ollama). Коммитит при успехе, откатывает при исключении.

    ВАЖНО: держать транзакцию КОРОТКОЙ — НИКАКИХ LLM/сетевых вызовов внутри,
    иначе заблокируешь всех писателей на время вызова. Делать только сами
    INSERT/UPDATE/DELETE, а тяжёлую работу (эмбеддинги, LLM) — ДО/ПОСЛЕ блока.
    """
    target = db_path or get_settings().db_path
    # isolation_level=None (autocommit) задаём при connect — в рабочем потоке
    # aiosqlite; ставить через свойство нельзя (cross-thread ProgrammingError).
    # В autocommit драйвер не делает неявных BEGIN, поэтому выдаём BEGIN IMMEDIATE сами.
    async with aiosqlite.connect(target, isolation_level=None) as conn:
        await _configure_connection(conn)
        wait_started = time.perf_counter()
        try:
            await conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            record_db_write_wait(
                time.perf_counter() - wait_started,
                acquired=False,
            )
            raise
        record_db_write_wait(
            time.perf_counter() - wait_started,
            acquired=True,
        )
        try:
            yield conn
            await conn.execute("COMMIT")
        except BaseException:
            try:
                await conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001, S110
                pass
            raise
