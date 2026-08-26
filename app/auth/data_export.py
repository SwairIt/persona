"""Само-обслуживаемый экспорт данных участника («право на доступ», 152-ФЗ ст. 14).

Инвариант файла
---------------
**Каждый** запрос здесь фильтруется по ``user_id`` действующего пользователя.
Ни одной выборки «всё подряд», ни одного VACUUM, ни одного глобального
``kv_settings``-дампа. Это принципиально другой объект, чем владельческий
``/settings/privacy/snapshot`` (тот отдаёт ВСЮ базу инстанса и остаётся
owner-only) — переиспользовать его нельзя ни при каких условиях.

Что попадает в выгрузку
-----------------------
Аккаунт, согласия, личные настройки, профиль, чаты с сообщениями, память,
рефлексии, навыки, граф знаний, друзья и заявки, личные сообщения (и
отправленные, и полученные), настройки ИИ-ответов и черновики, уведомления,
выдачи модели, счётчики использования, активные сессии, подписка и платежи,
устройства.

Что РЕДАКТИРУЕТСЯ и почему
--------------------------
1. ``users.password_hash`` — не выгружается вообще. Это верификатор пароля;
   в файле, который ляжет в «Загрузки», ему не место.
2. **Секреты в личных настройках** (``byo_api_key_*``, ``*_token``,
   ``*password*``, ``*secret*``): значение заменяется на маркер, рядом
   остаются ``present`` и ``length``. Обоснование: право на доступ — это
   право узнать, какие ПЕРСОНАЛЬНЫЕ ДАННЫЕ о тебе обрабатываются. Ключ
   OpenAI и токен Telegram-бота — не данные о человеке, а **действующие
   учётные данные**. Отдать их обратно по HTTP-скачиванию значит превратить
   «покажи мои данные» в примитив для кражи ключа: одной угнанной сессии или
   одного расшаренного файла хватит, чтобы забрать боевой ключ. Владелец
   ключа получил его у эмитента и всегда видит/меняет его на
   ``/settings/llm`` — то есть доступ к самому ключу мы не отнимаем, а лишь
   не размножаем его в переносимом артефакте. Факт наличия ключа и его длина
   в выгрузке остаются — этого хватает, чтобы убедиться, что мы храним.
3. ``auth_session.token`` и ``device.device_token`` — живые токены доступа,
   та же логика. Остаются время выпуска/истечения/отзыва и user-agent.
4. **Чужие e-mail** (друзья, собеседники, вторая сторона выдачи модели)
   маскируются до ``y***@domain``, ровно как это делает продукт в UI
   (``app/social/repository._mask_email``). Иначе выгрузка одного участника
   становится инструментом сбора адресной книги инстанса — это уже ЧУЖИЕ
   персональные данные, а право на доступ распространяется только на свои.
5. Тела ВХОДЯЩИХ личных сообщений выгружаются: это переписка участника,
   он её и так читает в ``/messages``. Автор чужой, адресат — он.

ШИФРОВАНИЕ (v2.33.x)
--------------------
Часть колонок лежит в базе зашифрованной (``app/member_crypto.py``): секреты
``user_settings``, тела ``dm_message``/``dm_ai_draft``, тексты уведомлений,
факты ``user_memory`` у не-владельца. Выгрузка обязана отдавать РАСШИФРОВАННОЕ:
право на доступ — это право прочитать свои данные, а файл с шифротекстом,
который человек не может открыть, этому праву не удовлетворяет. Единственное
исключение — секреты: они и раньше редактировались (пункт 2), и после
шифрования продолжают редактироваться. Зато ``length`` теперь считается по
РАСШИФРОВАННОМУ значению — иначе «длина ключа» показывала бы длину конверта.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.auth.consent import POLICY_VERSION, consent_rows, consent_state
from app.logging_setup import get_logger
from app.member_crypto import (
    SECRET_KEY_HINTS,
    decrypt_for_thread,
    decrypt_for_user,
    is_secret_setting_key,
)
from app.storage.db import get_connection

log = get_logger("persona.data_export")

#: Версия формата выгрузки. Растёт, когда меняется структура секций.
EXPORT_FORMAT_VERSION = 1

#: Чем заменяется значение секрета.
REDACTED = "«скрыто — см. раздел notes»"

#: Подстроки в ключе ``user_settings``, после которых значение не выгружается.
#: ЕДИНЫЙ список с тем, по которому значение шифруется при записи
#: (``app.member_crypto``): «что редактируем в выгрузке» и «что шифруем в базе»
#: обязаны совпадать — иначе появится секрет, который либо лежит открытым, либо
#: уезжает в файл.
_SECRET_HINTS: tuple[str, ...] = SECRET_KEY_HINTS


def _looks_secret(key: str) -> bool:
    return is_secret_setting_key(key)


def mask_email(email: str | None) -> str:
    """``yaroslav@gmail.com`` → ``y***@gmail.com``. Пусто → ``аноним``."""
    raw = (email or "").strip()
    local, sep, domain = raw.partition("@")
    if not sep or not local:
        return "аноним"
    return f"{local[0]}***@{domain}"


def _rows(cursor_rows: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor_rows]


async def _peer_names(conn: Any, ids: set[int]) -> dict[int, dict[str, str]]:
    """Витрина «кто это» для чужих id: маска адреса + display_name.

    Сырой e-mail сюда не попадает никогда — см. пункт 4 в шапке модуля.
    """
    out: dict[int, dict[str, str]] = {}
    clean = {int(i) for i in ids if i}
    if not clean:
        return out
    marks = ",".join("?" for _ in clean)
    cur = await conn.execute(
        f"SELECT id, email, display_name FROM users WHERE id IN ({marks})",  # noqa: S608
        tuple(clean),
    )
    for row in await cur.fetchall():
        out[int(row["id"])] = {
            "id": int(row["id"]),
            "email_masked": mask_email(row["email"]),
            "display_name": (row["display_name"] or "").strip() or None,
        }
    return out


async def _table(
    conn: Any, sql: str, params: tuple[Any, ...]
) -> list[dict[str, Any]]:
    """Выполнить выборку. Отсутствующая таблица → пустой список, не 500.

    Схема развивается миграциями; экспорт не имеет права падать целиком из-за
    одной таблицы, которой на этой БД ещё нет.
    """
    try:
        cur = await conn.execute(sql, params)
        return _rows(await cur.fetchall())
    except Exception as exc:  # noqa: BLE001
        log.debug("export.table_skipped", error=str(exc), sql=sql[:60])
        return []


async def _decrypted_notifications(conn: Any, uid: int) -> list[dict[str, Any]]:
    """Очередь браузерных уведомлений с расшифрованными телами."""
    rows = await _table(
        conn,
        "SELECT id, event, title, body, url, created_at, delivered_at "
        "FROM social_notif_item WHERE user_id = ? ORDER BY id",
        (uid,),
    )
    for row in rows:
        row["body"] = await decrypt_for_user(uid, row.get("body"), conn)
    return rows


async def build_export(user_id: int) -> dict[str, Any]:
    """Собрать полную выгрузку ОДНОГО пользователя. Всё фильтруется по ``user_id``."""
    uid = int(user_id)
    data: dict[str, Any] = {}

    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, email, display_name, role, status, created_at, last_login_at "
            "FROM users WHERE id = ?",
            (uid,),
        )
        row = await cur.fetchone()
        data["account"] = dict(row) if row else {}

        # --- личные настройки (user_settings) -------------------------------
        settings: list[dict[str, Any]] = []
        for r in await _table(
            conn,
            "SELECT key, value, updated_at FROM user_settings WHERE user_id = ? "
            "ORDER BY key",
            (uid,),
        ):
            key = str(r["key"])
            value = r["value"]
            if _looks_secret(key):
                # Расшифровываем ТОЛЬКО чтобы посчитать длину настоящего
                # значения; само значение в файл не попадает.
                plain = await decrypt_for_user(uid, value, conn)
                settings.append(
                    {
                        "key": key,
                        "value": REDACTED,
                        "redacted": True,
                        "present": bool(plain.strip()),
                        "length": len(plain),
                        "updated_at": r["updated_at"],
                    }
                )
            else:
                settings.append(
                    {"key": key, "value": value, "updated_at": r["updated_at"]}
                )
        data["settings"] = settings

        # --- «хвостатые» ключи глобального kv (user_profile_<uid> и др.) -----
        kv_out: dict[str, Any] = {}
        for kv_key in (f"user_profile_{uid}", f"onboarded_{uid}", f"email_verified_{uid}"):
            got = await _table(
                conn, "SELECT value FROM kv_settings WHERE key = ?", (kv_key,)
            )
            if got:
                kv_out[kv_key] = got[0]["value"]
        data["profile_and_flags"] = kv_out

        # --- согласия --------------------------------------------------------
        data["consent"] = {
            "current_policy_version": POLICY_VERSION,
            "state": await consent_state(uid),
            "records": await consent_rows(uid),
        }

        # --- чаты ------------------------------------------------------------
        sessions = await _table(
            conn,
            "SELECT id, title, provider, model, created_at, updated_at, summary, "
            "       custom_system_prompt "
            "FROM chat_session WHERE user_id = ? ORDER BY id",
            (uid,),
        )
        for s in sessions:
            s["messages"] = await _table(
                conn,
                "SELECT role, content, model_used, created_at, elapsed_ms, "
                "       input_tokens, output_tokens, is_pinned "
                "FROM chat_message WHERE session_id = ? ORDER BY id",
                (int(s["id"]),),
            )
            # kv-ключи режимов, привязанные к чату (живут в ГЛОБАЛЬНОМ kv).
            modes: dict[str, Any] = {}
            for suffix in ("chat_mode", "chat_effort"):
                got = await _table(
                    conn,
                    "SELECT value FROM kv_settings WHERE key = ?",
                    (f"{suffix}_{int(s['id'])}",),
                )
                if got:
                    modes[suffix] = got[0]["value"]
            s["modes"] = modes
        data["chats"] = sessions

        # --- память / рефлексии / навыки -------------------------------------
        memories = await _table(
            conn,
            "SELECT id, kind, text, pinned, created_at, updated_at, valid_until, "
            "       salience, redacted "
            "FROM user_memory WHERE user_id = ? ORDER BY id",
            (uid,),
        )
        for m in memories:
            m["text"] = await decrypt_for_user(uid, m.get("text"), conn)
        data["memories"] = memories
        data["reflections"] = await _table(
            conn,
            "SELECT id, kind, text, importance, valid_until, created_at "
            "FROM reflection WHERE user_id = ? ORDER BY id",
            (uid,),
        )
        data["skills"] = await _table(
            conn,
            "SELECT id, name, source_url, content, enabled, created_at "
            "FROM skill WHERE user_id = ? ORDER BY id",
            (uid,),
        )

        # --- граф знаний ------------------------------------------------------
        data["knowledge_graph"] = {
            "entities": await _table(
                conn,
                "SELECT id, name, kind, created_at FROM kg_entity "
                "WHERE user_id = ? ORDER BY id",
                (uid,),
            ),
            "edges": await _table(
                conn,
                "SELECT id, from_entity_id, to_entity_id, relation_type, strength, "
                "       source_kind, created_at, valid_until "
                "FROM kg_edge WHERE user_id = ? ORDER BY id",
                (uid,),
            ),
        }

        # --- социальный слой --------------------------------------------------
        friends = await _table(
            conn,
            "SELECT friend_id, created_at FROM friendship WHERE user_id = ? "
            "ORDER BY friend_id",
            (uid,),
        )
        requests = await _table(
            conn,
            "SELECT id, from_user_id, to_user_id, status, message, created_at, "
            "       responded_at "
            "FROM friend_request WHERE from_user_id = ? OR to_user_id = ? "
            "ORDER BY id",
            (uid, uid),
        )
        threads = await _table(
            conn,
            "SELECT id, user_a_id, user_b_id, created_at, last_message_at "
            "FROM dm_thread WHERE user_a_id = ? OR user_b_id = ? ORDER BY id",
            (uid, uid),
        )
        peer_ids: set[int] = {int(f["friend_id"]) for f in friends}
        for r in requests:
            peer_ids.update({int(r["from_user_id"]), int(r["to_user_id"])})
        for t in threads:
            peer_ids.update({int(t["user_a_id"]), int(t["user_b_id"])})

        grants_given = await _table(
            conn,
            "SELECT id, grantee_id, daily_limit, enabled, note, created_at, revoked_at "
            "FROM llm_grant WHERE grantor_id = ? ORDER BY id",
            (uid,),
        )
        grants_received = await _table(
            conn,
            "SELECT id, grantor_id, daily_limit, enabled, note, created_at, revoked_at "
            "FROM llm_grant WHERE grantee_id = ? ORDER BY id",
            (uid,),
        )
        peer_ids.update(int(g["grantee_id"]) for g in grants_given)
        peer_ids.update(int(g["grantor_id"]) for g in grants_received)

        ai_prefs = await _table(
            conn,
            "SELECT peer_id, mode, style_note, quota_daily, used_today, day, "
            "       auto_ack, updated_at "
            "FROM dm_ai_pref WHERE user_id = ? ORDER BY peer_id",
            (uid,),
        )
        peer_ids.update(int(p["peer_id"]) for p in ai_prefs)
        peer_ids.discard(uid)
        peers = await _peer_names(conn, peer_ids)

        def peer(pid: Any) -> dict[str, Any]:
            return peers.get(int(pid or 0), {"id": int(pid or 0), "email_masked": "аноним"})

        for f in friends:
            f["friend"] = peer(f.pop("friend_id"))
        for r in requests:
            r["direction"] = "outgoing" if int(r["from_user_id"]) == uid else "incoming"
            r["peer"] = peer(
                r["to_user_id"] if r["direction"] == "outgoing" else r["from_user_id"]
            )
            r.pop("from_user_id", None)
            r.pop("to_user_id", None)
        for g in grants_given:
            g["grantee"] = peer(g.pop("grantee_id"))
        for g in grants_received:
            g["grantor"] = peer(g.pop("grantor_id"))
        for p in ai_prefs:
            p["peer"] = peer(p.pop("peer_id"))

        for t in threads:
            other = int(t["user_b_id"]) if int(t["user_a_id"]) == uid else int(t["user_a_id"])
            t["peer"] = peer(other)
            t.pop("user_a_id", None)
            t.pop("user_b_id", None)
            msgs = await _table(
                conn,
                "SELECT id, sender_id, body, kind, created_at, read_at "
                "FROM dm_message WHERE thread_id = ? ORDER BY id",
                (int(t["id"]),),
            )
            for m in msgs:
                m["direction"] = "sent" if int(m["sender_id"]) == uid else "received"
                m.pop("sender_id", None)
                m["body"] = await decrypt_for_thread(int(t["id"]), m.get("body"), conn)
            t["messages"] = msgs
            drafts = await _table(
                conn,
                "SELECT body, reply_to_id, created_at FROM dm_ai_draft "
                "WHERE user_id = ? AND thread_id = ? ORDER BY created_at",
                (uid, int(t["id"])),
            )
            for d in drafts:
                d["body"] = await decrypt_for_thread(int(t["id"]), d.get("body"), conn)
            t["drafts"] = drafts

        data["social"] = {
            "friends": friends,
            "friend_requests": requests,
            "dm_threads": threads,
            "dm_ai_preferences": ai_prefs,
        }

        # --- уведомления -------------------------------------------------------
        data["notifications"] = {
            "preferences": await _table(
                conn,
                "SELECT event, channel, enabled, updated_at FROM social_notif_pref "
                "WHERE user_id = ? ORDER BY event, channel",
                (uid,),
            ),
            "queue": await _decrypted_notifications(conn, uid),
            "cooldowns": await _table(
                conn,
                "SELECT scope, last_sent_at FROM social_notif_cooldown "
                "WHERE user_id = ? ORDER BY scope",
                (uid,),
            ),
        }

        # --- выдачи модели + расход -------------------------------------------
        grant_ids = [int(g["id"]) for g in grants_given] + [
            int(g["id"]) for g in grants_received
        ]
        grant_usage: list[dict[str, Any]] = []
        for gid in grant_ids:
            grant_usage.extend(
                await _table(
                    conn,
                    "SELECT grant_id, day, used FROM llm_grant_usage WHERE grant_id = ? "
                    "ORDER BY day",
                    (gid,),
                )
            )
        data["llm_grants"] = {
            "given": grants_given,
            "received": grants_received,
            "usage": grant_usage,
        }

        # --- счётчики использования (агрегат, не тела запросов) ---------------
        data["llm_usage"] = await _table(
            conn,
            "SELECT kind, provider, COUNT(*) AS calls, "
            "       SUM(COALESCE(input_tokens, 0)) AS input_tokens, "
            "       SUM(COALESCE(output_tokens, 0)) AS output_tokens, "
            "       SUM(success) AS successes "
            "FROM llm_usage WHERE user_id = ? GROUP BY kind, provider "
            "ORDER BY kind, provider",
            (uid,),
        )

        # --- сессии и устройства (без живых токенов) ---------------------------
        data["sessions"] = await _table(
            conn,
            "SELECT id, issued_at, expires_at, revoked_at, user_agent, last_seen_at "
            "FROM auth_session WHERE user_id = ? ORDER BY id",
            (uid,),
        )
        data["devices"] = await _table(
            conn,
            "SELECT id, name, kind, capture_paused, capture_interval_seconds, "
            "       created_at, last_seen_at, user_agent "
            "FROM device WHERE user_id = ? ORDER BY id",
            (uid,),
        )

        # --- биллинг ------------------------------------------------------------
        data["billing"] = {
            "subscriptions": await _table(
                conn,
                "SELECT id, plan, billing_cycle, status, provider, amount, currency, "
                "       current_period_start, current_period_end, "
                "       cancel_at_period_end, created_at "
                "FROM subscription WHERE user_id = ? ORDER BY id",
                (uid,),
            ),
            "payments": await _table(
                conn,
                "SELECT id, provider, kind, amount, currency, status, period_start, "
                "       period_end, description, created_at "
                "FROM payment WHERE user_id = ? ORDER BY id",
                (uid,),
            ),
        }

        # --- голос / реакции ------------------------------------------------------
        data["voice"] = await _table(
            conn,
            "SELECT id, text, voice, status, created_at, completed_at FROM voice_tts "
            "WHERE user_id = ? ORDER BY id",
            (uid,),
        )
        data["chat_reactions"] = await _table(
            conn,
            "SELECT id, message_id, reaction, created_at FROM chat_reaction "
            "WHERE user_id = ? ORDER BY id",
            (uid,),
        )

    data["meta"] = {
        "format_version": EXPORT_FORMAT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "user_id": uid,
        "policy_version": POLICY_VERSION,
        "notes": [
            "Выгрузка построена ТОЛЬКО из строк, привязанных к этому аккаунту.",
            "Пароль (его хэш) не выгружается: это верификатор входа, а не данные о тебе.",
            "Ключи API, токены ботов, токены сессий и устройств заменены на маркер: "
            "отдавать действующие учётные данные файлом небезопасно. Свой ключ "
            "всегда видно и можно сменить на /settings/llm.",
            "Адреса других людей показаны маской (y***@domain): чужой e-mail — "
            "это персональные данные другого человека, а не твои.",
            "Входящие личные сообщения включены: это твоя переписка, ты читаешь её в /messages.",
        ],
    }
    # meta первым ключом — так удобнее открывать файл глазами.
    return {"meta": data.pop("meta"), **data}


def export_json_bytes(payload: dict[str, Any]) -> bytes:
    """JSON-байты выгрузки: UTF-8, с отступами, кириллица не экранируется."""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


async def export_counts(user_id: int) -> dict[str, int]:
    """Лёгкие счётчики для страницы «Мои данные» (без сборки всей выгрузки)."""
    uid = int(user_id)
    out: dict[str, int] = {}
    async with get_connection() as conn:
        for name, sql in (
            ("chats", "SELECT COUNT(*) AS n FROM chat_session WHERE user_id = ?"),
            (
                "messages",
                "SELECT COUNT(*) AS n FROM chat_message m "
                "JOIN chat_session s ON s.id = m.session_id WHERE s.user_id = ?",
            ),
            ("memories", "SELECT COUNT(*) AS n FROM user_memory WHERE user_id = ?"),
            ("skills", "SELECT COUNT(*) AS n FROM skill WHERE user_id = ?"),
            ("friends", "SELECT COUNT(*) AS n FROM friendship WHERE user_id = ?"),
            (
                "dm_messages",
                "SELECT COUNT(*) AS n FROM dm_message WHERE sender_id = ?",
            ),
        ):
            got = await _table(conn, sql, (uid,))
            out[name] = int(got[0]["n"]) if got else 0
    return out


__all__ = [
    "EXPORT_FORMAT_VERSION",
    "REDACTED",
    "build_export",
    "export_counts",
    "export_json_bytes",
    "mask_email",
]
