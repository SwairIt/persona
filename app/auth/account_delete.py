"""Самостоятельное удаление аккаунта участником («право на удаление», 152-ФЗ).

Что делает
----------
:func:`delete_own_account` стирает ВСЕ строки одного пользователя и сам
``users``-ряд, отзывает его сессии и оставляет обезличенную запись в
``account_deletion_log`` (только id + время + счётчики), чтобы оператор мог
доказать исполнение требования, не храня при этом ничего о человеке.

Кого удалять НЕЛЬЗЯ
-------------------
Владельца инстанса. Гард повторяет паттерн ``app/auth/roles.set_status``:
владелец резолвится и по kv ``owner_user_id`` (:func:`app.auth.owner.is_owner`),
и по колонке ``users.role``; последний ``owner`` не удаляется никогда. Без
этого один клик в кабинете обезглавливал бы инстанс: пропали бы kv-владельца,
и ``auth_gate`` начал бы отдавать приватную поверхность кому попало.

────────────────────────────────────────────────────────────────────────────
ИНВЕНТАРИЗАЦИЯ КАСКАДА (снято с реальной схемы: PRAGMA foreign_key_list)
────────────────────────────────────────────────────────────────────────────
``PRAGMA foreign_keys = ON`` включён на каждом соединении (app/storage/db.py),
поэтому часть таблиц уезжает сама. Список ниже — не литература, а контракт:
если в схеме появится новая таблица с ``user_id``, её надо внести сюда.

A. САМИ уезжают по ON DELETE CASCADE от ``users(id)``:
     auth_session, chat_session, device, dm_ai_draft(user_id),
     dm_ai_pref(user_id, peer_id), dm_message(sender_id),
     dm_thread(user_a_id, user_b_id), dream_report, dream_run,
     dynamic_system_prompt_config, dynamic_system_prompt_version,
     friend_request(from_user_id, to_user_id), friendship(user_id, friend_id),
     llm_grant(grantor_id, grantee_id), memory_projection_outbox,
     memory_revision_embedding, payment, reflection, social_notif_cooldown,
     social_notif_item, social_notif_pref, subscription, sync_event,
     telegram_person, telegram_pinned_chat, telegram_pinned_message,
     user_memory, user_settings, user_consent, worker_enrollment_ticket,
     workspace_file_event.

B. Уезжают по ЦЕПОЧКЕ от таблиц группы A:
     chat_message      → chat_session(id)  CASCADE
     tool_execution    → chat_session(id)  CASCADE
     dm_message        → dm_thread(id)     CASCADE
     dm_ai_draft       → dm_thread(id)     CASCADE
     llm_grant_usage   → llm_grant(id)     CASCADE
     kg_edge           → kg_entity(id)     CASCADE
     telegram_person_* → telegram_person   CASCADE

C. НЕ уезжают — колонка есть, внешнего ключа НЕТ. Только явный DELETE:
     agent_fs_command(user_id), chat_reaction(user_id),
     dream_privacy_purge_guard(user_id), entity(user_id), kg_edge(user_id),
     kg_entity(user_id), llm_job(user_id), llm_usage(user_id), skill(user_id),
     tool_execution(user_id), vec_message_meta(user_id), voice_tts(user_id),
     persona_thought(persona_user_id), persona_thought_chain(persona_user_id),
     telegram_pending_action(persona_user_id),
     telegram_person_fact/_message/_override(persona_user_id),
     autowake_event/_outbox/_session(owner_user_id),
     remote_browser_session/_job(owner_user_id).

D. ЛОВУШКА: ``training_dataset.session_id`` → chat_session ``ON DELETE SET
   NULL``. Строка НЕ удаляется — в ней остаётся ПОЛНЫЙ текст пары «вопрос
   пользователя / ответ модели». Каскад бы её осиротил и сохранил. Удаляем
   явно ПЕРВОЙ, до чатов.

E. ЛОВУШКА: ``kv_settings`` — глобальная таблица «ключ → значение», внешних
   ключей у неё нет вообще. Персональные ключи там живут с суффиксом:
     user_profile_<uid>, onboarded_<uid>, email_verified_<uid>
   и посессионные ключи чатов (суффикс — id ЧАТА, не пользователя):
     chat_mode_<sid>, chat_effort_<sid>, chat_stop_<sid>
   Их не заберёт ни один каскад — только явный DELETE, см. :func:`_kv_keys_for`.

F. FTS5-зеркала (``chat_message_fts``) синхронизируются ТРИГГЕРАМИ на
   chat_message. Поэтому сообщения удаляются ЯВНЫМ ``DELETE FROM
   chat_message``, а не «за компанию» через каскад от chat_session: срабатывание
   пользовательских триггеров на FK-действиях зависит от
   ``PRAGMA recursive_triggers``, и полагаться на это для стирания текста
   нельзя. То же для ``chat_message_vec`` (виртуальная таблица sqlite-vec,
   существует только если расширение установлено — удаление в try/except).

────────────────────────────────────────────────────────────────────────────
ПОЛИТИКА ПО ЛИЧНЫМ СООБЩЕНИЯМ: ЖЁСТКОЕ УДАЛЕНИЕ У ОБЕИХ СТОРОН
────────────────────────────────────────────────────────────────────────────
Конфликт настоящий: сообщения, которые уходящий написал, — его персональные
данные, а копия у получателя — часть переписки получателя. Выбран вариант
«снести ветку целиком у обоих», а не «обезличить отправителя».

Почему так:

1. **Схема уже так и устроена.** ``dm_message.sender_id`` — ``NOT NULL
   REFERENCES users(id) ON DELETE CASCADE``, а ``dm_thread`` каскадирует по
   обеим сторонам. Обезличивание потребовало бы пересборки таблицы dm_message
   (SQLite не умеет ALTER для смены NOT NULL/FK) — то есть миграции по чужому
   социальному слою ради результата, который всё равно хуже, см. п. 2.
2. **Обезличенный огрызок — это не удаление.** Останутся время, частота,
   порядок реплик и содержимое сообщений уходящего. По метаданным переписка
   один-на-один тривиально атрибутируется обратно: собеседник и так знает, с
   кем говорил. Отзыв согласия (ст. 9 ч. 2) обязан снимать именно это.
3. **Ущерб получателя ограничен и предсказуем.** Он теряет свои реплики в
   ОДНОЙ ветке с ушедшим человеком, а не аккаунт, не друзей и не остальные
   переписки. Это цена, которую платит собеседник ушедшего, и в UI удаления
   она названа прямым текстом («переписка исчезнет у обоих»).

Что это НЕ покрывает: получателя мы предупредить не можем — уведомления ему
не шлём (иначе факт удаления аккаунта сам стал бы рассылкой). Он просто
увидит, что ветки больше нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.auth.owner import is_owner
from app.auth.roles import owner_count
from app.auth.sessions import revoke_all_for_user
from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction

log = get_logger("persona.account_delete")

#: Причины отказа — стабильные ключи, роут переводит их в текст.
REFUSE_OWNER = "owner_not_deletable"
REFUSE_UNKNOWN = "user_not_found"
REFUSE_CONFIRM = "confirmation_mismatch"

#: Группа C инвентаризации: таблица → колонка с id пользователя.
#: Внешнего ключа на users у них нет, каскад их не заберёт.
_EXPLICIT_USER_TABLES: tuple[tuple[str, str], ...] = (
    ("agent_fs_command", "user_id"),
    ("chat_reaction", "user_id"),
    ("dream_privacy_purge_guard", "user_id"),
    ("entity", "user_id"),
    ("kg_edge", "user_id"),
    ("kg_entity", "user_id"),
    ("llm_job", "user_id"),
    ("llm_usage", "user_id"),
    ("skill", "user_id"),
    ("tool_execution", "user_id"),
    ("vec_message_meta", "user_id"),
    ("voice_tts", "user_id"),
    ("persona_thought", "persona_user_id"),
    ("persona_thought_chain", "persona_user_id"),
    ("telegram_pending_action", "persona_user_id"),
    ("telegram_person_fact", "persona_user_id"),
    ("telegram_person_message", "persona_user_id"),
    ("telegram_person_override", "persona_user_id"),
    ("autowake_event", "owner_user_id"),
    ("autowake_outbox", "owner_user_id"),
    ("autowake_session", "owner_user_id"),
    ("remote_browser_job", "owner_user_id"),
    ("remote_browser_session", "owner_user_id"),
)


@dataclass(slots=True)
class DeletionResult:
    """Итог попытки удаления. ``ok=False`` + ``reason`` — отказ."""

    ok: bool
    reason: str | None = None
    rows_deleted: int = 0
    kv_keys_deleted: int = 0
    per_table: dict[str, int] = field(default_factory=dict)


def _kv_keys_for(user_id: int, session_ids: list[int]) -> list[str]:
    """Ключи ГЛОБАЛЬНОГО ``kv_settings``, принадлежащие этому человеку.

    Каскад до них не дотягивается (у kv_settings нет ни одного FK), поэтому
    список ведётся руками. Ключи чатов суффиксованы id ЧАТА, а не id
    пользователя — их приходится собирать по его сессиям заранее, ПОКА они
    ещё существуют.
    """
    keys = [
        f"user_profile_{user_id}",
        f"onboarded_{user_id}",
        f"email_verified_{user_id}",
    ]
    for sid in session_ids:
        keys.extend((f"chat_mode_{sid}", f"chat_effort_{sid}", f"chat_stop_{sid}"))
    return keys


async def can_delete(user_id: int) -> tuple[bool, str | None]:
    """Можно ли удалить этот аккаунт этим способом. Гард владельца.

    Владелец режется дважды — по kv ``owner_user_id`` и по ``users.role``,
    ровно как ``roles.set_status`` отказывается снять последнего owner.
    """
    uid = int(user_id)
    async with get_connection() as conn:
        cur = await conn.execute("SELECT role FROM users WHERE id = ?", (uid,))
        row = await cur.fetchone()
    if row is None:
        return False, REFUSE_UNKNOWN
    if await is_owner(uid):
        return False, REFUSE_OWNER
    if str(row["role"] or "") == "owner" and await owner_count() <= 1:
        return False, REFUSE_OWNER
    return True, None


async def delete_own_account(user_id: int) -> DeletionResult:
    """Удалить аккаунт и ВСЕ его данные. Порядок операций важен — см. шапку.

    Личные сообщения удаляются жёстко у обеих сторон: ветка переписки исчезает
    и у уходящего, и у собеседника (обоснование — блок «ПОЛИТИКА ПО ЛИЧНЫМ
    СООБЩЕНИЯМ» в docstring модуля).
    """
    uid = int(user_id)
    allowed, reason = await can_delete(uid)
    if not allowed:
        log.warning("account_delete.refused", user_id=uid, reason=reason)
        return DeletionResult(ok=False, reason=reason)

    # Сессии гасим ДО удаления строк: даже если транзакция ниже упадёт, живой
    # cookie уже не пустит обратно, а пользователь увидит форму входа.
    await revoke_all_for_user(uid)

    per_table: dict[str, int] = {}
    total = 0
    kv_deleted = 0

    async with write_transaction() as conn:
        cur = await conn.execute(
            "SELECT id FROM chat_session WHERE user_id = ?", (uid,)
        )
        session_ids = [int(r["id"]) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT id FROM dm_thread WHERE user_a_id = ? OR user_b_id = ?",
            (uid, uid),
        )
        thread_ids = [int(r["id"]) for r in await cur.fetchall()]
        message_ids: list[int] = []
        if session_ids:
            marks = ",".join("?" for _ in session_ids)
            cur = await conn.execute(
                f"SELECT id FROM chat_message WHERE session_id IN ({marks})",  # noqa: S608
                tuple(session_ids),
            )
            message_ids = [int(r["id"]) for r in await cur.fetchall()]

        async def run(table: str, sql: str, params: tuple[object, ...]) -> None:
            nonlocal total
            try:
                cur_ = await conn.execute(sql, params)
            except Exception as exc:  # noqa: BLE001 — таблицы может не быть
                log.debug("account_delete.skip", table=table, error=str(exc))
                return
            n = int(cur_.rowcount or 0)
            if n > 0:
                per_table[table] = per_table.get(table, 0) + n
                total += n

        # D — ловушка SET NULL: обучающие пары с ПОЛНЫМ текстом переписки.
        for sid in session_ids:
            await run(
                "training_dataset",
                "DELETE FROM training_dataset WHERE session_id = ?",
                (sid,),
            )
        for mid in message_ids:
            await run(
                "training_dataset",
                "DELETE FROM training_dataset WHERE user_message_id = ? "
                "OR asst_message_id = ?",
                (mid, mid),
            )

        # F — векторное зеркало сообщений (есть только с sqlite-vec).
        for mid in message_ids:
            await run(
                "chat_message_vec",
                "DELETE FROM chat_message_vec WHERE rowid = ?",
                (mid,),
            )

        # F — сообщения чата явным DELETE, чтобы отработали FTS-триггеры.
        for sid in session_ids:
            await run(
                "chat_message", "DELETE FROM chat_message WHERE session_id = ?", (sid,)
            )
            await run(
                "tool_execution",
                "DELETE FROM tool_execution WHERE session_id = ?",
                (sid,),
            )
        await run("chat_session", "DELETE FROM chat_session WHERE user_id = ?", (uid,))

        # Личные сообщения: ветка целиком, у обеих сторон.
        for tid in thread_ids:
            await run("dm_message", "DELETE FROM dm_message WHERE thread_id = ?", (tid,))
            await run(
                "dm_ai_draft", "DELETE FROM dm_ai_draft WHERE thread_id = ?", (tid,)
            )
            await run("dm_thread", "DELETE FROM dm_thread WHERE id = ?", (tid,))
        # Настройки ИИ-ответов: и его про других, и чужие про него.
        await run(
            "dm_ai_pref",
            "DELETE FROM dm_ai_pref WHERE user_id = ? OR peer_id = ?",
            (uid, uid),
        )

        # C — таблицы без внешнего ключа на users.
        for table, column in _EXPLICIT_USER_TABLES:
            await run(table, f"DELETE FROM {table} WHERE {column} = ?", (uid,))  # noqa: S608

        # E — «хвостатые» ключи глобального kv.
        for key in _kv_keys_for(uid, session_ids):
            try:
                cur_ = await conn.execute(
                    "DELETE FROM kv_settings WHERE key = ?", (key,)
                )
                kv_deleted += int(cur_.rowcount or 0)
            except Exception as exc:  # noqa: BLE001
                log.debug("account_delete.kv_skip", key=key, error=str(exc))

        # A — сам ряд users. Каскад добирает всё остальное.
        await run("users", "DELETE FROM users WHERE id = ?", (uid,))

        # Журнал исполнения: только id, время и счётчики. Ни адреса, ни текста.
        try:
            await conn.execute(
                "INSERT INTO account_deletion_log "
                "(user_id, initiated_by, rows_deleted, kv_keys_deleted) "
                "VALUES (?, 'self', ?, ?)",
                (uid, total, kv_deleted),
            )
        except Exception as exc:  # noqa: BLE001 — журнал не отменяет удаление
            log.warning("account_delete.log_failed", user_id=uid, error=str(exc))

    log.info(
        "account_delete.done",
        user_id=uid,
        rows_deleted=total,
        kv_keys_deleted=kv_deleted,
    )
    return DeletionResult(
        ok=True,
        rows_deleted=total,
        kv_keys_deleted=kv_deleted,
        per_table=per_table,
    )


__all__ = [
    "REFUSE_CONFIRM",
    "REFUSE_OWNER",
    "REFUSE_UNKNOWN",
    "DeletionResult",
    "can_delete",
    "delete_own_account",
]
