"""Шифрование данных участников «в покое» — проверяемая гарантия, а не обещание.

Что здесь закрепляется (см. docs/MEMBER_ENCRYPTION.md):

1. **Файл базы бесполезен сам по себе.** Секрет, личное сообщение и факт
   памяти не находятся в файлах БД grep'ом — ни в ``persona.db``, ни в WAL.
2. **Продукт при этом работает.** Тот же ключ доезжает до ``make_client`` и
   поднимает клиента с ПРАВИЛЬНЫМ ключом, переписка читается, память попадает
   в промпт.
3. **Второй участник не расшифрует первого**, даже имея полный доступ к базе:
   ключи разные и завёрнуты мастер-ключом, которого в базе нет.
4. **Удаление аккаунта уносит ключ** (крипто-шреддинг) вместе со строками.
5. **Выгрузка отдаёт читаемое** — иначе право на доступ не исполнено.
6. **Legacy plaintext переживает апгрейд** и добирается фоновым добором.
7. **Нет ключа / чужой ключ → деградация, а не 500.**
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import pytest_asyncio

from app import member_crypto
from app.auth import owner as owner_mod
from app.auth.account_delete import delete_own_account
from app.auth.data_export import REDACTED, build_export
from app.auth.users import create_user
from app.chat import user_memory
from app.llm.client import OpenRouterClient, make_client
from app.social import ai_pref, notifications
from app.social import repository as social
from app.storage.db import get_connection
from app.storage.repository import get_user_kv, set_kv, set_user_kv

# Канарейки: строки, которых нет ни в шаблонах, ни в переводах, поэтому любое
# совпадение в файле базы — настоящая утечка, а не ложная тревога.
C_KEY = "sk-or-KANAREYKA-MEMBER-KEY-77"
C_DM = "KANAREYKA-LICHNOE-SOOBSHENIE-78"
C_FACT = "KANAREYKA-FAKT-PAMYATI-79"
C_DRAFT = "KANAREYKA-CHERNOVIK-80"
C_NOTIF = "KANAREYKA-UVEDOMLENIE-81"


def _reset_owner_cache() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0


@pytest_asyncio.fixture
async def people(db: aiosqlite.Connection) -> dict[str, int]:
    """Владелец + два участника (A и B)."""
    owner = await create_user("owner@enc.test", "Zq7-frost-lantern-91")
    a = await create_user("a@enc.test", "Kp4-velvet-harbour-38", "Аня")
    b = await create_user("b@enc.test", "Kp4-velvet-harbour-38", "Боря")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    _reset_owner_cache()
    return {"owner": int(owner["id"]), "a": int(a["id"]), "b": int(b["id"])}


async def _raw(sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
    """Прочитать базу В ОБХОД приложения — ровно как это сделал бы владелец."""
    async with get_connection() as conn:
        cursor = await conn.execute(sql, params)
        return list(await cursor.fetchall())


def _db_bytes() -> bytes:
    """Все файлы базы (включая WAL) одним куском — «дай мне файл» глазами вора."""
    from app.settings import get_settings

    base = Path(get_settings().db_path)
    blob = b""
    for path in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
        if path.exists():
            blob += path.read_bytes()
    return blob


async def _befriend(a: int, b: int) -> int:
    request_id = await social.send_request(a, b)
    assert await social.accept_request(request_id, b)
    return await social.get_or_create_thread(a, b)


# ── 1. Секрет в базе — шифротекст ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_stored_secret_is_not_plaintext_in_the_database(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)

    rows = await _raw(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
        (people["a"], "byo_api_key_openrouter"),
    )
    assert rows, "строка настройки не сохранилась"
    stored = str(rows[0]["value"])
    assert stored.startswith(member_crypto.PREFIX), "секрет лежит в базе открытым текстом"
    assert C_KEY not in stored

    # И его нет НИГДЕ в файлах базы — включая WAL, куда попадает свежая запись.
    assert C_KEY.encode() not in _db_bytes(), "ключ участника находится grep'ом по файлу БД"


@pytest.mark.asyncio
async def test_non_secret_setting_stays_readable(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Шифруются ТОЛЬКО секреты: тема/язык остаются обычными строками.

    Это не косметика: ``templates_engine.get_user_kv_sync`` читает такие ключи
    синхронно и мимо расшифровки. Зашифруй мы их — сломалась бы тема.
    """
    await set_user_kv(db, people["a"], "theme", "cosmos")
    rows = await _raw(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = 'theme'",
        (people["a"],),
    )
    assert str(rows[0]["value"]) == "cosmos"


# ── 2. Продукт продолжает работать ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_path_gets_the_real_key_back(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Ключ шифруется на записи и расшифровывается там, где им платят за токены."""
    await set_user_kv(db, people["a"], "llm_provider", "openrouter")
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)

    assert await get_user_kv(db, people["a"], "byo_api_key_openrouter") == C_KEY

    client = make_client(kind="chat", user_id=people["a"])
    inner = getattr(client, "_inner", client)
    assert isinstance(inner, OpenRouterClient)
    assert inner._api_key == C_KEY


@pytest.mark.asyncio
async def test_direct_message_is_encrypted_but_readable_by_both(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    thread_id = await _befriend(people["a"], people["b"])
    await social.send_message(thread_id, people["a"], C_DM)

    rows = await _raw("SELECT body FROM dm_message WHERE thread_id = ?", (thread_id,))
    assert str(rows[0]["body"]).startswith(member_crypto.PREFIX)
    assert C_DM.encode() not in _db_bytes(), "тело личного сообщения лежит в файле БД открытым"

    for uid in (people["a"], people["b"]):
        messages = await social.list_messages(thread_id, uid)
        assert [m["body"] for m in messages] == [C_DM]

    # Превью в списке переписок — тот же текст, тоже расшифрованный.
    cards = await social.list_threads(people["b"])
    assert cards and cards[0]["last_body"] == C_DM


@pytest.mark.asyncio
async def test_draft_and_notification_bodies_are_encrypted(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Черновик и цитата в уведомлении — тот же личный текст, что и сообщение."""
    thread_id = await _befriend(people["a"], people["b"])
    await ai_pref.save_draft(people["a"], thread_id, C_DRAFT)
    await notifications.queue_browser(people["b"], "dm_message", "Сообщение", C_NOTIF)

    blob = _db_bytes()
    assert C_DRAFT.encode() not in blob
    assert C_NOTIF.encode() not in blob

    draft = await ai_pref.get_draft(people["a"], thread_id)
    assert draft is not None and draft["body"] == C_DRAFT
    pending = await notifications.take_pending(people["b"])
    assert [n["body"] for n in pending] == [C_NOTIF]


@pytest.mark.asyncio
async def test_member_memory_is_encrypted_and_owner_memory_is_not(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Память участника — шифротекст; память ВЛАДЕЛЬЦА намеренно остаётся текстом.

    Владельческие сны/проекции/граф читают ``user_memory.text`` напрямую в SQL;
    шифровать данные владельца от него же самого нечего, а сломать этим можно
    многое (обоснование — docs/MEMBER_ENCRYPTION.md).
    """
    await user_memory.add_memory(people["a"], C_FACT)
    await user_memory.add_memory(people["owner"], "владельческий факт")

    member_rows = await _raw("SELECT text FROM user_memory WHERE user_id = ?", (people["a"],))
    owner_rows = await _raw("SELECT text FROM user_memory WHERE user_id = ?", (people["owner"],))
    assert str(member_rows[0]["text"]).startswith(member_crypto.PREFIX)
    assert str(owner_rows[0]["text"]) == "владельческий факт"
    assert C_FACT.encode() not in _db_bytes()

    # Чтение, поиск и блок для промпта видят открытый текст.
    assert [m["text"] for m in await user_memory.list_memory(people["a"])] == [C_FACT]
    assert await user_memory.search_memory(people["a"], "KANAREYKA")
    assert C_FACT in await user_memory.build_memory_block(people["a"])

    # Дедуп работает и на шифротексте (сравнение переехало в Python).
    await user_memory.add_memory(people["a"], C_FACT)
    assert len(await user_memory.list_memory(people["a"])) == 1, "дедуп не увидел дубль"


@pytest.mark.asyncio
async def test_memory_edit_keeps_ciphertext(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    mem_id = await user_memory.add_memory(people["a"], C_FACT)
    assert mem_id is not None
    assert await user_memory.edit_memory(people["a"], mem_id, "исправленный факт")
    rows = await _raw("SELECT text FROM user_memory WHERE id = ?", (mem_id,))
    assert str(rows[0]["text"]).startswith(member_crypto.PREFIX)
    assert [m["text"] for m in await user_memory.list_memory(people["a"])] == ["исправленный факт"]


# ── 3. Второй участник не расшифрует первого ────────────────────────────────


@pytest.mark.asyncio
async def test_second_member_cannot_decrypt_the_first(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """У B полный доступ к базе — и это ему не помогает."""
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    await set_user_kv(db, people["b"], "byo_api_key_openrouter", "sk-or-B-KEY")

    rows = await _raw(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
        (people["a"], "byo_api_key_openrouter"),
    )
    a_ciphertext = str(rows[0]["value"])

    # Расшифровка чужого конверта СВОИМ ключом не даёт ни исходника, ни ошибки —
    # тег не сходится, значение пустое.
    assert await member_crypto.decrypt("user", people["b"], a_ciphertext) == ""
    assert await member_crypto.decrypt_for_user(people["a"], a_ciphertext) == C_KEY

    # И завёрнутые ключи в базе — разные строки.
    keys = await _raw("SELECT user_id, wrapped_key FROM user_encryption_key ORDER BY user_id")
    wrapped = {int(r["user_id"]): bytes(r["wrapped_key"]) for r in keys}
    assert wrapped[people["a"]] != wrapped[people["b"]]


# ── 4. Удаление ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deletion_removes_the_key_and_the_rows(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    await user_memory.add_memory(people["a"], C_FACT)
    thread_id = await _befriend(people["a"], people["b"])
    await social.send_message(thread_id, people["a"], C_DM)

    assert await _raw(
        "SELECT user_id FROM user_encryption_key WHERE user_id = ?", (people["a"],)
    )

    result = await delete_own_account(people["a"])
    assert result.ok, result.reason

    assert not await _raw(
        "SELECT user_id FROM user_encryption_key WHERE user_id = ?", (people["a"],)
    ), "ключ пережил удаление аккаунта — крипто-шреддинг не сработал"
    assert not await _raw(
        "SELECT key FROM user_settings WHERE user_id = ?", (people["a"],)
    )
    assert not await _raw("SELECT id FROM user_memory WHERE user_id = ?", (people["a"],))
    # Ветка переписки уносит и свой ключ (каскад от dm_thread).
    assert not await _raw("SELECT thread_id FROM dm_thread_key WHERE thread_id = ?", (thread_id,))


# ── 5. Выгрузка ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_is_readable_by_the_member(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    await user_memory.add_memory(people["a"], C_FACT)
    thread_id = await _befriend(people["a"], people["b"])
    await social.send_message(thread_id, people["a"], C_DM)
    await ai_pref.save_draft(people["a"], thread_id, C_DRAFT)
    await notifications.queue_browser(people["a"], "dm_message", "Сообщение", C_NOTIF)

    export = await build_export(people["a"])

    assert [m["text"] for m in export["memories"]] == [C_FACT]
    thread = export["social"]["dm_threads"][0]
    assert [m["body"] for m in thread["messages"]] == [C_DM]
    assert [d["body"] for d in thread["drafts"]] == [C_DRAFT]
    assert [n["body"] for n in export["notifications"]["queue"]] == [C_NOTIF]

    # Секрет по-прежнему редактируется, но длина считается по НАСТОЯЩЕМУ значению.
    secret = next(s for s in export["settings"] if s["key"] == "byo_api_key_openrouter")
    assert secret["value"] == REDACTED
    assert secret["present"] is True
    assert secret["length"] == len(C_KEY)


# ── 6. Legacy plaintext ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_plaintext_rows_still_work(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Строка без маркера — это данные, записанные до апгрейда. Отдаём как есть."""
    await db.execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
        (people["a"], "byo_api_key_openrouter", C_KEY),
    )
    await db.execute(
        "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
        (people["a"], C_FACT),
    )
    await db.commit()

    assert await get_user_kv(db, people["a"], "byo_api_key_openrouter") == C_KEY
    assert [m["text"] for m in await user_memory.list_memory(people["a"])] == [C_FACT]


@pytest.mark.asyncio
async def test_backfill_encrypts_legacy_rows(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    from app.member_crypto_backfill import run_backfill

    thread_id = await _befriend(people["a"], people["b"])
    await db.execute(
        "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
        (people["a"], "byo_api_key_openrouter", C_KEY),
    )
    await db.execute(
        "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
        (people["a"], C_FACT),
    )
    await db.execute(
        "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
        (people["owner"], "владельческий факт"),
    )
    await db.execute(
        "INSERT INTO dm_message (thread_id, sender_id, body) VALUES (?, ?, ?)",
        (thread_id, people["a"], C_DM),
    )
    await db.commit()

    report = await run_backfill()
    assert report["status"] == "ok", report

    blob = _db_bytes()
    assert C_KEY.encode() not in blob
    assert C_FACT.encode() not in blob
    assert C_DM.encode() not in blob
    # Владелец не тронут.
    assert "владельческий факт".encode() in blob

    # И всё это по-прежнему читается приложением.
    assert await get_user_kv(db, people["a"], "byo_api_key_openrouter") == C_KEY
    assert [m["text"] for m in await user_memory.list_memory(people["a"])] == [C_FACT]
    assert [m["body"] for m in await social.list_messages(thread_id, people["a"])] == [C_DM]

    # Идемпотентность: второй прогон ничего не переписывает.
    again = await run_backfill()
    assert again["shared"] == "already_done"


# ── 7. Деградация вместо 500 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_key_degrades_to_plaintext_write(
    db: aiosqlite.Connection, people: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Нет ключа — пишем открытым текстом и НЕ теряем данные (и не падаем)."""
    monkeypatch.setattr(member_crypto, "master_key", lambda: None)
    member_crypto._dek_cache.clear()

    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    assert await get_user_kv(db, people["a"], "byo_api_key_openrouter") == C_KEY
    rows = await _raw(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
        (people["a"], "byo_api_key_openrouter"),
    )
    assert str(rows[0]["value"]) == C_KEY  # честно открытым текстом, а не «упало»


@pytest.mark.asyncio
async def test_wrong_key_reads_empty_instead_of_500(
    db: aiosqlite.Connection, people: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ключ подменили (потеряли файл, восстановили не тот) — пустое поле, не крах."""
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    thread_id = await _befriend(people["a"], people["b"])
    await social.send_message(thread_id, people["a"], C_DM)

    # Другой мастер-ключ = ничего не разворачивается.
    monkeypatch.setattr(member_crypto, "master_key", lambda: b"\x11" * 32)
    member_crypto._dek_cache.clear()

    assert await get_user_kv(db, people["a"], "byo_api_key_openrouter") == ""
    assert [m["body"] for m in await social.list_messages(thread_id, people["a"])] == [""]
    assert await user_memory.list_memory(people["a"]) == []


@pytest.mark.asyncio
async def test_tampered_ciphertext_is_rejected(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Правка шифротекста в базе не подменяет значение — тег не сойдётся."""
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    rows = await _raw(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
        (people["a"], "byo_api_key_openrouter"),
    )
    stored = str(rows[0]["value"])
    flipped = stored[:-2] + ("AA" if stored[-2:] != "AA" else "BB")
    await db.execute(
        "UPDATE user_settings SET value = ? WHERE user_id = ? AND key = ?",
        (flipped, people["a"], "byo_api_key_openrouter"),
    )
    await db.commit()

    assert await get_user_kv(db, people["a"], "byo_api_key_openrouter") == ""


@pytest.mark.asyncio
async def test_key_file_lives_outside_the_database(
    db: aiosqlite.Connection, people: dict[str, int]
) -> None:
    """Ключ не в базе — иначе всё это упражнение бессмысленно."""
    await set_user_kv(db, people["a"], "byo_api_key_openrouter", C_KEY)
    path = member_crypto.keyring_path()
    assert path.exists(), "мастер-ключ не создан"
    assert path.read_bytes() not in _db_bytes()
    # И его нет ни в одной таблице настроек.
    kv_rows = await _raw("SELECT value FROM kv_settings")
    assert path.read_text("utf-8").strip() not in {str(r["value"]).strip() for r in kv_rows}
