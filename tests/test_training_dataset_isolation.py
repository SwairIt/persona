# ruff: noqa: RUF001, RUF002, RUF003
"""Датасет дообучения — владельческий. Переписки участников в нём нет.

Что тут закрывается
-------------------
``training_dataset`` (миграция 162) хранит ПОЛНЫЙ текст пары «вопрос / ответ»
плюс системный промпт и предыдущие реплики, пишется после КАЖДОГО ответа
ассистента, сбор включён по умолчанию, а владельческий экспорт
``/admin/dataset/export.jsonl`` до 2026-08-26 выбирал таблицу целиком и без
единого условия по пользователю. С открытой публичной регистрацией это значит:
личный разговор постороннего человека уезжал владельцу файлом и дальше — в веса
дообученной модели.

Тесты держат ДВА независимых рубежа (см. app/training/collector.py):

1. ЗАПИСЬ — :func:`record_qa_pair` (настоящая точка входа коллектора, её же
   зовёт роут чата и ``ops/selfplay.py``) для не-владельца не создаёт строку
   вовсе. И — контрольная проверка — для владельца по-прежнему создаёт: утечку
   нельзя «закрыть», сломав фичу.
2. ЧТЕНИЕ — :func:`iter_export_rows` и :func:`stats` фильтруют по владельцу
   ДАЖЕ ЕСЛИ чужая строка каким-то образом в таблице оказалась. Поэтому чужая
   строка тут вставляется в обход коллектора, напрямую: так рубеж 2
   проверяется независимо от рубежа 1.

Плюс: удаление аккаунта уносит его строки, и миграция 236 действительно
проставляет ``user_id`` и вычищает чужое и неатрибутируемое.
"""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

import app.training.collector as collector
from app.auth import owner as owner_mod
from app.auth.account_delete import delete_own_account
from app.auth.users import create_user
from app.chat import append_message, create_session
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.training import iter_export_rows, record_qa_pair, stats

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_MIGRATION_236 = (
    Path(__file__).resolve().parents[1]
    / "app" / "storage" / "migrations" / "236_training_dataset_user_id.sql"
)

#: Канарейки: строки уникальны, поэтому их появление в выгрузке — настоящая
#: утечка, а не совпадение с шаблоном или переводом.
OWNER_Q = "KANAREYKA-TRAIN-OWNER-Q-01"
OWNER_A = "KANAREYKA-TRAIN-OWNER-A-02"
MEMBER_Q = "KANAREYKA-TRAIN-MEMBER-Q-03"
MEMBER_A = "KANAREYKA-TRAIN-MEMBER-A-04"


def _reset_owner_cache() -> None:
    """Резолв владельца кэшируется в процессе на 60с — гасим между тестами."""
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0


@pytest_asyncio.fixture
async def instance() -> AsyncIterator[dict[str, int]]:
    """Инстанс с владельцем и участником; сбор датасета ВКЛЮЧЁН (как в бою)."""
    await init_database()
    owner = await create_user("train-owner-01@iso.test", "Zx7-Alpha-Passphrase")
    member = await create_user("train-member-02@iso.test", "Qw4-Bravo-Passphrase")
    async with get_connection() as conn:
        await set_kv(conn, "owner_user_id", str(owner["id"]))
        await set_kv(conn, "full_access_user_ids", "")
        await set_kv(conn, "training_dataset_enabled", "1")
    _reset_owner_cache()
    yield {"owner": int(owner["id"]), "member": int(member["id"])}
    _reset_owner_cache()


async def _turn(user_id: int, question: str, answer: str) -> int | None:
    """Один ход чата ЧЕРЕЗ НАСТОЯЩУЮ точку входа коллектора."""
    thread = await create_session(user_id, title="ветка")
    session_id = int(thread["id"])
    user_msg = await append_message(session_id, "user", question)
    asst_msg = await append_message(session_id, "assistant", answer)
    return await record_qa_pair(
        user_id=user_id,
        session_id=session_id,
        user_message_id=int(user_msg["id"]),
        asst_message_id=int(asst_msg["id"]),
        user_text=question,
        assistant_text=answer,
        system_prompt="ты ассистент",
        context_turns=[{"role": "user", "content": "прошлая реплика"}],
        image_present=False,
        provider="ollama",
        model="qwen2.5:3b",
    )


async def _rows() -> list[dict[str, object]]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, user_id, session_id, user_text FROM training_dataset"
        )
        return [dict(r) for r in await cur.fetchall()]


# ── Рубеж 1: запись ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_turn_records_nothing(instance: dict[str, int]) -> None:
    """Ход участника не оставляет в датасете НИ ОДНОЙ строки."""
    row_id = await _turn(instance["member"], MEMBER_Q, MEMBER_A)
    assert row_id is None, "коллектор отчитался о записи строки участника"
    assert await _rows() == [], "переписка участника попала в training_dataset"


@pytest.mark.asyncio
async def test_owner_turn_still_records(instance: dict[str, int]) -> None:
    """Фича жива: ход владельца по-прежнему попадает в датасет, с автором."""
    row_id = await _turn(instance["owner"], OWNER_Q, OWNER_A)
    assert row_id, "ход владельца перестал записываться — фича сломана"
    rows = await _rows()
    assert len(rows) == 1
    assert rows[0]["user_id"] == instance["owner"]
    assert rows[0]["user_text"] == OWNER_Q


@pytest.mark.asyncio
async def test_full_access_delegate_records(instance: dict[str, int]) -> None:
    """Делегат из kv ``full_access_user_ids`` считается владельцем — как везде.

    Проверка нужна, чтобы гейт и фильтр выгрузки не разъехались: оба обязаны
    считать владельцем ОДНО И ТО ЖЕ множество id.
    """
    async with get_connection() as conn:
        await set_kv(conn, "full_access_user_ids", str(instance["member"]))
    _reset_owner_cache()
    assert await _turn(instance["member"], MEMBER_Q, MEMBER_A)
    exported = await iter_export_rows()
    assert any(MEMBER_Q in str(item) for item in exported)


@pytest.mark.asyncio
async def test_user_id_is_a_required_argument() -> None:
    """``user_id`` — обязательный именованный параметр без значения по умолчанию.

    Так новая точка вызова не сможет молча унаследовать «автор неизвестен»:
    без аргумента вызов падает, а не пишет чужой текст.
    """
    param = inspect.signature(record_qa_pair).parameters["user_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


@pytest.mark.asyncio
async def test_collection_flag_cannot_re_enable_member_capture(
    instance: dict[str, int],
) -> None:
    """Даже с явно включённым флагом сбора чужой ход не пишется."""
    async with get_connection() as conn:
        await set_kv(conn, "training_dataset_enabled", "1")
    assert await _turn(instance["member"], MEMBER_Q, MEMBER_A) is None
    assert await _rows() == []


# ── Рубеж 2: чтение (независимо от рубежа 1) ────────────────────────────────


@pytest.mark.asyncio
async def test_export_skips_a_force_inserted_member_row(
    instance: dict[str, int],
) -> None:
    """Строка участника, вставленная В ОБХОД коллектора, в экспорт не идёт.

    Это доказывает фильтр выгрузки сам по себе: если завтра кто-то вернёт
    запись чужих строк, файл дообучения всё равно останется владельческим.
    """
    await _turn(instance["owner"], OWNER_Q, OWNER_A)

    thread = await create_session(instance["member"], title="чужая ветка")
    async with get_connection() as conn:
        # Обе формы, в которых чужой текст может оказаться в таблице: с
        # user_id участника и «старая» строка вообще без автора.
        await conn.execute(
            "INSERT INTO training_dataset "
            "  (user_id, session_id, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?)",
            (instance["member"], int(thread["id"]), MEMBER_Q, MEMBER_A),
        )
        await conn.execute(
            "INSERT INTO training_dataset "
            "  (user_id, session_id, user_text, assistant_text) "
            "VALUES (NULL, ?, ?, ?)",
            (int(thread["id"]), MEMBER_Q + "-NULL", MEMBER_A),
        )
        await conn.commit()

    exported = await iter_export_rows()
    blob = str(exported)
    assert OWNER_Q in blob, "выгрузка владельца потеряла его собственные строки"
    assert MEMBER_Q not in blob, "чужая переписка попала в экспорт датасета"
    assert MEMBER_A not in blob

    info = await stats()
    assert info["total"] == 1, "статистика считает чужие строки как свои"


@pytest.mark.asyncio
async def test_export_is_empty_when_owner_unresolvable(
    instance: dict[str, int],
) -> None:
    """Не смогли решить, кто владелец → пустая выдача, а не вся таблица."""
    await _turn(instance["owner"], OWNER_Q, OWNER_A)

    async def _no_owner() -> set[int]:
        return set()

    original = collector.owner_user_ids
    collector.owner_user_ids = _no_owner  # type: ignore[assignment]
    try:
        assert await iter_export_rows() == []
        assert (await stats())["total"] == 0
    finally:
        collector.owner_user_ids = original  # type: ignore[assignment]


# ── Удаление аккаунта ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_account_deletion_removes_training_rows(
    instance: dict[str, int],
) -> None:
    """Строки участника уходят вместе с аккаунтом — во всех адресациях.

    Схема поменялась (появился ``user_id``), поэтому проверяем заново: строка,
    привязанная к чату, и строка, у которой чат уже отвязан (``ON DELETE SET
    NULL``) — её можно найти ТОЛЬКО по автору.
    """
    thread = await create_session(instance["member"], title="ветка")
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO training_dataset "
            "  (user_id, session_id, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?)",
            (instance["member"], int(thread["id"]), MEMBER_Q, MEMBER_A),
        )
        # Осиротевшая строка: сессии нет, сообщений нет — только автор.
        await conn.execute(
            "INSERT INTO training_dataset "
            "  (user_id, session_id, user_text, assistant_text) "
            "VALUES (?, NULL, ?, ?)",
            (instance["member"], MEMBER_Q + "-ORPHAN", MEMBER_A),
        )
        await conn.commit()

    result = await delete_own_account(instance["member"])
    assert result.ok, result.reason

    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM training_dataset WHERE user_text LIKE ?",
            (MEMBER_Q + "%",),
        )
        row = await cur.fetchone()
        assert int(row["n"]) == 0


# ── Миграция 236 ────────────────────────────────────────────────────────────


def _seed_pre_236(path: Path) -> sqlite3.Connection:
    """База в форме ДО миграции 236: у training_dataset нет ``user_id``."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE kv_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT);
        CREATE TABLE chat_session (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL
        );
        CREATE TABLE training_dataset (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER REFERENCES chat_session(id) ON DELETE SET NULL,
            user_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL
        );
        """
    )
    return db


def test_migration_backfills_owner_and_purges_the_rest(tmp_path: Path) -> None:
    """Миграция: проставить автора, оставить владельца, удалить чужое и ничьё."""
    db = _seed_pre_236(tmp_path / "pre236.db")
    db.executescript(
        """
        INSERT INTO users (id, email) VALUES (5, 'owner@t'), (6, 'member@t'),
                                            (7, 'delegate@t');
        INSERT INTO kv_settings (key, value) VALUES
            ('owner_user_id', '5'), ('full_access_user_ids', '7');
        INSERT INTO chat_session (id, user_id) VALUES (10, 5), (11, 6), (12, 7);
        INSERT INTO training_dataset (session_id, user_text, assistant_text) VALUES
            (10, 'owner-q', 'owner-a'),
            (11, 'member-q', 'member-a'),
            (12, 'delegate-q', 'delegate-a'),
            (NULL, 'orphan-q', 'orphan-a'),
            (99,  'dangling-q', 'dangling-a');
        """
    )
    db.commit()
    db.executescript(_MIGRATION_236.read_text(encoding="utf-8"))
    db.commit()

    left = dict(
        db.execute("SELECT user_text, user_id FROM training_dataset").fetchall()
    )
    assert left == {"owner-q": 5, "delegate-q": 7}, (
        "миграция оставила чужие или неатрибутируемые строки: " + repr(left)
    )
    db.close()


def test_migration_falls_back_to_lowest_user_id(tmp_path: Path) -> None:
    """Без kv ``owner_user_id`` владелец — младший id, как в рантайме."""
    db = _seed_pre_236(tmp_path / "pre236b.db")
    db.executescript(
        """
        INSERT INTO users (id, email) VALUES (3, 'owner@t'), (9, 'member@t');
        INSERT INTO chat_session (id, user_id) VALUES (20, 3), (21, 9);
        INSERT INTO training_dataset (session_id, user_text, assistant_text) VALUES
            (20, 'owner-q', 'owner-a'),
            (21, 'member-q', 'member-a');
        """
    )
    db.commit()
    db.executescript(_MIGRATION_236.read_text(encoding="utf-8"))
    db.commit()
    left = dict(
        db.execute("SELECT user_text, user_id FROM training_dataset").fetchall()
    )
    assert left == {"owner-q": 3}
    db.close()
