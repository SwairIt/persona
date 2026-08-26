"""Разовый добор: зашифровать строки, записанные ДО включения шифрования.

Зачем отдельный модуль, а не SQL в миграции
-------------------------------------------
Миграция не может зашифровать: ключ лежит ВНЕ базы
(``$PERSONA_DATA_DIR/member_keyring.key``), а SQLite про него ничего не знает.
Поэтому добор — это код, который запускается один раз при старте
(``app/bootstrap/lifespan.py``) и переписывает legacy-строки конвертами.

Почему это обязательно, а не «на будущее»
-----------------------------------------
Наполовину зашифрованная таблица — худший исход из возможных: обещание
«переписка зашифрована» формально верно для новых строк и полностью ложно для
старых, и никто не знает, каких сколько. После добора состояние однозначно:
всё, что не начинается с ``pcenc1:``, — либо не подлежит шифрованию, либо
писалось в момент, когда ключа не было (это видно в логах).

Свойства
--------
* **Идемпотентен.** Строку с маркером не трогает. Повторный запуск — no-op.
* **Помечает себя в kv**, чтобы не сканировать таблицы на каждом старте.
* **Не роняет старт.** Любая ошибка — лог и выход; продукт поднимается.
* **Память владельца не трогает** (см. ``member_crypto.encrypts_memory_for``);
  если владелец ещё не резолвится, стадия памяти НЕ помечается выполненной и
  повторится на следующем старте — «не смогли» лучше, чем «пропустили молча».
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app import member_crypto
from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    import aiosqlite

log = get_logger("persona.member_crypto.backfill")

#: kv-флаги «стадия выполнена». Версия в имени: если формат конверта сменится,
#: заведётся ``_v2`` и добор пройдёт заново.
FLAG_SHARED = "member_encryption_backfill_v1"
FLAG_MEMORY = "member_encryption_memory_backfill_v1"


async def _flag(conn: aiosqlite.Connection, name: str) -> bool:
    cursor = await conn.execute("SELECT value FROM kv_settings WHERE key = ?", (name,))
    row = await cursor.fetchone()
    return bool(row) and str(row["value"]).strip() == "1"


async def _set_flag(conn: aiosqlite.Connection, name: str) -> None:
    await conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) VALUES (?, '1', datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = datetime('now')",
        (name,),
    )


async def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    """Выборка, которая переживает отсутствие таблицы (старая БД)."""
    try:
        cursor = await conn.execute(sql, params)
        return list(await cursor.fetchall())
    except Exception as exc:  # noqa: BLE001 — таблицы может не быть
        log.debug("backfill.table_skipped", error=str(exc), sql=sql[:60])
        return []


async def _encrypt_shared(conn: aiosqlite.Connection) -> dict[str, int] | None:
    """Секреты настроек, переписка, черновики, уведомления. Владельца тоже.

    ``None`` — ключ пропал посреди работы: стадию НЕ помечаем выполненной,
    иначе оставшиеся строки навсегда останутся открытыми и никто об этом не
    узнает.
    """
    done: dict[str, int] = {}

    # 1. user_settings: только секреты (ключи API, токены, пароли).
    count = 0
    for row in await _rows(conn, "SELECT user_id, key, value FROM user_settings"):
        key = str(row["key"])
        value = str(row["value"] or "")
        if not value or member_crypto.is_ciphertext(value):
            continue
        if not member_crypto.is_secret_setting_key(key):
            continue
        sealed = await member_crypto.encrypt_for_user(int(row["user_id"]), value, conn)
        if not member_crypto.is_ciphertext(sealed):
            return None  # ключа нет — прекращаем, флаг не ставим
        await conn.execute(
            "UPDATE user_settings SET value = ? WHERE user_id = ? AND key = ?",
            (sealed, int(row["user_id"]), key),
        )
        count += 1
    done["user_settings"] = count

    # 2. dm_message / dm_ai_draft — ключ ВЕТКИ.
    for table in ("dm_message", "dm_ai_draft"):
        count = 0
        rows = await _rows(conn, f"SELECT rowid AS rid, thread_id, body FROM {table}")  # noqa: S608
        for row in rows:
            body = str(row["body"] or "")
            if not body or member_crypto.is_ciphertext(body):
                continue
            sealed = await member_crypto.encrypt_for_thread(int(row["thread_id"]), body, conn)
            if not member_crypto.is_ciphertext(sealed):
                return None
            await conn.execute(
                f"UPDATE {table} SET body = ? WHERE rowid = ?",  # noqa: S608 — имя из литерала
                (sealed, int(row["rid"])),
            )
            count += 1
        done[table] = count

    # 3. social_notif_item — ключ ПОЛУЧАТЕЛЯ.
    count = 0
    for row in await _rows(conn, "SELECT id, user_id, body FROM social_notif_item"):
        body = str(row["body"] or "")
        if not body or member_crypto.is_ciphertext(body):
            continue
        sealed = await member_crypto.encrypt_for_user(int(row["user_id"]), body, conn)
        if not member_crypto.is_ciphertext(sealed):
            return None
        await conn.execute(
            "UPDATE social_notif_item SET body = ? WHERE id = ?",
            (sealed, int(row["id"])),
        )
        count += 1
    done["social_notif_item"] = count
    return done


async def _encrypt_memory(conn: aiosqlite.Connection) -> int | None:
    """Факты ``user_memory`` НЕ-владельцев. ``None`` — владелец не резолвится."""
    from app.auth.owner import owner_user_ids  # noqa: PLC0415 — цикл импорта

    owners = await owner_user_ids()
    if not owners:
        # Пустое множество = «владелец неизвестен». Зашифровав сейчас, мы
        # рискуем зашифровать память ВЛАДЕЛЬЦА и сломать ему сны/проекции.
        return None

    count = 0
    for row in await _rows(conn, "SELECT id, user_id, text FROM user_memory"):
        uid = int(row["user_id"] or 0)
        text = str(row["text"] or "")
        if not uid or uid in owners or not text or member_crypto.is_ciphertext(text):
            continue
        sealed = await member_crypto.encrypt_for_user(uid, text, conn)
        if not member_crypto.is_ciphertext(sealed):
            return None
        await conn.execute("UPDATE user_memory SET text = ? WHERE id = ?", (sealed, int(row["id"])))
        count += 1
    return count


async def _scrub_freed_pages() -> bool:
    """Вычистить из ФАЙЛА страницы со старым открытым текстом (checkpoint + VACUUM).

    Без этого шага добор — половина работы, и притом самая обманчивая: ``UPDATE``
    переписывает строку, но СТАРАЯ страница с открытым текстом остаётся в файле
    (freelist) и в WAL, то есть ``grep`` по ``persona.db`` продолжает находить
    ключ, который «уже зашифрован». Именно это поймал
    ``tests/test_member_encryption.py::test_backfill_encrypts_legacy_rows``.

    ``wal_checkpoint(TRUNCATE)`` сливает и обнуляет WAL, ``VACUUM`` пересобирает
    файл без свободных страниц. Дорого — поэтому ровно один раз, только если
    что-то реально переписали.

    Честная граница: VACUUM убирает данные из ФАЙЛА, но не затирает сектора
    диска и не трогает уже сделанные резервные копии. Старый бэкап, снятый до
    апгрейда, остаётся открытым текстом — его нужно удалить руками.
    """
    import aiosqlite  # noqa: PLC0415 — старт-онли

    from app.settings import get_settings  # noqa: PLC0415

    try:
        async with aiosqlite.connect(get_settings().db_path, isolation_level=None) as conn:
            # Курсор PRAGMA обязан быть ВЫЧЕРПАН и закрыт: незакрытый набор
            # строк — это «SQL statements in progress», и VACUUM отказывается.
            async def checkpoint() -> None:
                # Курсор PRAGMA обязан быть ВЫЧЕРПАН и закрыт: незакрытый набор
                # строк — это «SQL statements in progress», и VACUUM откажется.
                cursor = await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await cursor.fetchall()
                await cursor.close()

            # Порядок важен и стоил одного красного теста. В режиме WAL запись
            # VACUUM уходит В WAL, а старые страницы основного файла остаются на
            # месте — поэтому checkpoint нужен И ДО (слить хвост в файл, чтобы
            # VACUUM его переписал), И ПОСЛЕ (перенести результат VACUUM в файл
            # и обнулить WAL). Без второго вызова открытый текст продолжает
            # находиться grep'ом по паре ``persona.db`` + ``persona.db-wal``.
            await checkpoint()
            await conn.execute("VACUUM")
            await checkpoint()
    except Exception as exc:  # noqa: BLE001 — занятая БД: повторим на следующем старте
        log.warning("backfill.scrub_failed", error=str(exc))
        return False
    log.info("backfill.scrubbed")
    return True


def _rewritten(value: Any) -> int:
    """Сколько строк реально переписано на этой стадии."""
    if isinstance(value, dict):
        return sum(int(n) for n in value.values())
    return int(value) if isinstance(value, int) else 0


async def run_backfill() -> dict[str, Any]:
    """Прогнать добор. Возвращает отчёт (для логов и тестов)."""
    report: dict[str, Any] = {"status": "ok"}
    if not member_crypto.encryption_available():
        log.error("backfill.no_key", path=str(member_crypto.keyring_path()))
        return {"status": "no_key"}

    from app.storage.db import write_transaction  # noqa: PLC0415 — старт-онли

    async with write_transaction() as conn:
        if await _flag(conn, FLAG_SHARED):
            report["shared"] = "already_done"
        else:
            shared = await _encrypt_shared(conn)
            if shared is None:
                report["shared"] = "deferred_no_key"
            else:
                report["shared"] = shared
                await _set_flag(conn, FLAG_SHARED)

        if await _flag(conn, FLAG_MEMORY):
            report["memory"] = "already_done"
        else:
            done = await _encrypt_memory(conn)
            if done is None:
                # Владельца ещё нет (свежая установка). Повторим на следующем
                # старте — флаг не ставим.
                report["memory"] = "deferred_owner_unknown"
            else:
                report["memory"] = done
                await _set_flag(conn, FLAG_MEMORY)

    rewritten = _rewritten(report.get("shared")) + _rewritten(report.get("memory"))
    if rewritten:
        report["scrubbed"] = await _scrub_freed_pages()
    log.info("backfill.done", **{k: str(v) for k, v in report.items()})
    return report


__all__ = ["FLAG_MEMORY", "FLAG_SHARED", "run_backfill"]
