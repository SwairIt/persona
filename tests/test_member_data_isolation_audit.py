"""Сквозной аудит изоляции данных: участник не видит НИЧЕГО чужого.

Чем этот файл отличается от соседей
-----------------------------------
* ``test_member_surface`` / ``test_member_settings_isolation`` — роуты-заглушки,
  проверяется решение ГЕЙТА и адрес записи настроек.
* ``test_mvp_smoke_audit`` — реальное приложение, 8 канареек владельца, обход
  member-поверхности БЕЗ параметров пути.
* **этот файл** — то же реальное приложение, но:
  1. поверхность берётся ИЗ ПРИЛОЖЕНИЯ и включает роуты С параметрами пути —
     ``{session_id}``/``{thread_id}``/``{message_id}``/… подставляются РЕАЛЬНЫМИ
     id владельца и ВТОРОГО участника (IDOR-обход, а не только листинги);
  2. канареек ~20 и они закрывают ВСЕ owner-global таблицы из брифа
     (screenshots+OCR, аудио, часовые/дневные/недельные карточки, дайджест,
     заметки, напоминания, теги, сохранённые поиски, брифинг, мысль, telegram-
     человек, чат+память+промпт владельца) ПЛЮС отличительные значения
     глобального ``kv_settings`` (токены/пароли/пути/имена моделей);
  3. отдельным блоком — изоляция УЧАСТНИК-vs-УЧАСТНИК (member B): его чаты,
     память, навыки, переписка, черновик ИИ, уведомления, выдача модели;
  4. POST-фаззинг id-принимающих эндпоинтов чужими id: 403/404, но НИКОГДА
     200 с чужими данными;
  5. контрольные проверки ВЛАДЕЛЬЦА — чтобы «утечка закрыта» нельзя было
     получить, сломав фичу всем.

kv ``owner_exclusive_mode`` тут ВЫКЛ (kill-switch проверяется отдельно в
``test_owner_exclusive_lockdown``).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner as owner_mod
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv, set_user_kv
from app.web import rate_limit, templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import _is_member_path
from app.web.routes import setup_gate

# ── Канарейки ───────────────────────────────────────────────────────────────
# Каждая строка уникальна и не встречается в шаблонах/переводах, поэтому
# любое совпадение в теле ответа — настоящая утечка, а не ложная тревога.

# --- личные данные ВЛАДЕЛЬЦА (owner-global таблицы) ---
C_SCREENSHOT = "KANAREYKA-SHOT-ZW01"
C_OCR = "KANAREYKA-OCR-ZW02"
C_AUDIO = "KANAREYKA-AUDIO-ZW03"
C_HOURLY = "KANAREYKA-HOUR-ZW04"
C_TRANSCRIPT = "KANAREYKA-RECH-ZW05"
C_DAILY_PIN = "KANAREYKA-DAYPIN-ZW06"
C_WEEKLY = "KANAREYKA-WEEK-ZW07"
C_DIGEST = "KANAREYKA-DIGEST-ZW08"
C_NOTE = "KANAREYKA-NOTE-ZW09"
C_REMINDER = "KANAREYKA-REMIND-ZW10"
C_TAG = "KANAREYKA-TAG-ZW11"
C_SAVED_SEARCH = "KANAREYKA-SAVEDSEARCH-ZW12"
C_BRIEFING = "KANAREYKA-BRIEF-ZW13"
C_THOUGHT = "KANAREYKA-THOUGHT-ZW14"
C_TELEGRAM = "KANAREYKA-TGPERSON-ZW15"
C_CLIPBOARD = "KANAREYKA-CLIP-ZW16"
C_OWNER_CHAT = "KANAREYKA-CHAT-OWNER-ZW17"
C_OWNER_MEMORY = "KANAREYKA-MEMORY-OWNER-ZW18"
C_OWNER_PROMPT = "KANAREYKA-PROMPT-OWNER-ZW19"
C_OWNER_SKILL = "KANAREYKA-SKILL-OWNER-ZW20"
C_OWNER_ENTITY = "KANAREYKA-ENTITY-OWNER-ZW21"
C_OWNER_REFLECTION = "KANAREYKA-REFLECT-OWNER-ZW22"
# --- отличительные значения ГЛОБАЛЬНОГО kv (секреты инфраструктуры) ---
C_KV_TG_TOKEN = "KANAREYKA-TGTOKEN-ZW23"
C_KV_SMTP = "KANAREYKA-SMTPPASS-ZW24"
C_KV_BRAVE = "KANAREYKA-BRAVEKEY-ZW25"
C_KV_MACPATH = "KANAREYKA-MACPATH-ZW26"
C_KV_WORKERMODEL = "kanareyka-worker-model-zw27"
C_KV_OLLAMA_URL = "kanareyka-ollama-host-zw28"
C_KV_TRAIN = "KANAREYKA-TRAINRESULT-ZW29"
# E-mail владельца целиком. Продукт маскирует адреса ВЕЗДЕ, где показывает
# человека (``_mask_email`` в app/social/repository.py), поэтому полный адрес в
# ответе участнику — утечка, даже если они друзья.
C_OWNER_EMAIL = "zw30-kanareyka-owner@iso-audit.test"

OWNER_CANARIES: tuple[str, ...] = (
    C_SCREENSHOT, C_OCR, C_AUDIO, C_HOURLY, C_TRANSCRIPT, C_DAILY_PIN,
    C_WEEKLY, C_DIGEST, C_NOTE, C_REMINDER, C_TAG, C_SAVED_SEARCH,
    C_BRIEFING, C_THOUGHT, C_TELEGRAM, C_CLIPBOARD, C_OWNER_CHAT,
    C_OWNER_MEMORY, C_OWNER_PROMPT, C_OWNER_SKILL, C_OWNER_ENTITY,
    C_OWNER_REFLECTION, C_KV_TG_TOKEN, C_KV_SMTP, C_KV_BRAVE,
    C_KV_MACPATH, C_KV_WORKERMODEL, C_KV_OLLAMA_URL, C_KV_TRAIN,
    C_OWNER_EMAIL,
)

# --- данные ВТОРОГО УЧАСТНИКА (member B) ---
B_CHAT = "KANAREYKA-CHAT-BEE-YB01"
B_MESSAGE = "KANAREYKA-MSG-BEE-YB02"
B_MEMORY = "KANAREYKA-MEMORY-BEE-YB03"
B_SKILL = "KANAREYKA-SKILL-BEE-YB04"
B_PROMPT = "KANAREYKA-PROMPT-BEE-YB05"
B_DM = "KANAREYKA-DM-BEE-YB06"
B_DRAFT = "KANAREYKA-DRAFT-BEE-YB07"
B_NOTIF = "KANAREYKA-NOTIF-BEE-YB08"
B_GRANT_NOTE = "KANAREYKA-GRANT-BEE-YB09"
B_APIKEY = "sk-kanareyka-bee-yb10"
B_TG_TOKEN = "111111:KANAREYKA-BEE-BOTTOKEN-YB11"
B_REFLECTION = "KANAREYKA-REFLECT-BEE-YB12"
B_ENTITY = "KANAREYKA-ENTITY-BEE-YB13"
B_EMAIL = "yb14-kanareyka-bee@iso-audit.test"

MEMBER_B_CANARIES: tuple[str, ...] = (
    B_CHAT, B_MESSAGE, B_MEMORY, B_SKILL, B_PROMPT, B_DM, B_DRAFT,
    B_NOTIF, B_GRANT_NOTE, B_APIKEY, B_TG_TOKEN, B_REFLECTION, B_ENTITY,
    B_EMAIL,
)

ALL_CANARIES: tuple[str, ...] = OWNER_CANARIES + MEMBER_B_CANARIES


def _reset_caches() -> None:
    """Сбросить процесс-глобальные TTL-кэши личности/темы/языка.

    В каждом тесте своя временная БД, а кэши модульные — без сброса решение
    гейта или тема протекают между тестами.
    """
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0
    templates_engine._kv_value_cache.clear()
    templates_engine._user_kv_value_cache.clear()
    templates_engine.invalidate_theme_cache()
    i18n.invalidate_language_cache()
    # Лимитер держит счётчики в модульном dict по ключу «действие:user_id», а
    # user_id в каждом тесте один и тот же (БД пересоздаётся) — без сброса
    # второй тест ловит 429 от первого.
    rate_limit._EVENTS.clear()


# ── Посев данных ────────────────────────────────────────────────────────────


async def _seed_owner(owner_id: int) -> dict[str, int]:
    """Богатый набор личных данных владельца. Возвращает id-шники для IDOR."""
    ids: dict[str, int] = {}
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO screenshots "
            "(captured_at, monitor_index, width, height, phash, app_name, window_title) "
            "VALUES ('2026-08-20T10:15:00', 0, 1920, 1080, ?, 'Telegram', ?)",
            (C_SCREENSHOT, C_SCREENSHOT),
        )
        shot_id = int(cur.lastrowid or 0)
        ids["screenshot"] = shot_id
        await conn.execute(
            "INSERT INTO ocr_word (screenshot_id, word, conf, left, top, width, height) "
            "VALUES (?, ?, 99, 1, 1, 10, 10)",
            (shot_id, C_OCR),
        )
        await conn.execute(
            "INSERT INTO audio_segment "
            "(captured_at, ended_at, duration_seconds, codec, path, size_bytes, transcript) "
            "VALUES ('2026-08-20T10:20:00', '2026-08-20T10:21:00', 60.0, 'opus', "
            "        '/tmp/canary.opus', 1024, ?)",
            (C_AUDIO,),
        )
        await conn.execute(
            "INSERT INTO hourly_card "
            "(hour_start, hour_end, summary, screen_count, audio_seconds, "
            " transcript_excerpt, llm_enriched, created_at) "
            "VALUES ('2026-08-20T10:00:00', '2026-08-20T10:59:59', ?, 12, 300, ?, 0, "
            "        datetime('now'))",
            (C_HOURLY, C_TRANSCRIPT),
        )
        await conn.execute(
            "INSERT INTO daily_pin (day, pin, apps) VALUES ('2026-08-20', ?, 'Telegram')",
            (C_DAILY_PIN,),
        )
        await conn.execute(
            "INSERT INTO weekly_card (week_start, week_end, summary) "
            "VALUES ('2026-08-17', '2026-08-23', ?)",
            (C_WEEKLY,),
        )
        await conn.execute(
            "INSERT INTO daily_digest (day, body) VALUES ('2026-08-20', ?)",
            (C_DIGEST,),
        )
        cur = await conn.execute(
            "INSERT INTO notes (body, created_at, updated_at) "
            "VALUES (?, datetime('now'), datetime('now'))",
            (C_NOTE,),
        )
        ids["note"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO reminders (body, due_date) VALUES (?, '2026-08-25T09:00:00')",
            (C_REMINDER,),
        )
        ids["reminder"] = int(cur.lastrowid or 0)
        cur = await conn.execute("INSERT INTO tags (name) VALUES (?)", (C_TAG,))
        tag_id = int(cur.lastrowid or 0)
        # Тег попадает в палитру только если им что-то помечено (HAVING n > 0).
        await conn.execute(
            "INSERT INTO screenshot_tags (screenshot_id, tag_id) VALUES (?, ?)",
            (shot_id, tag_id),
        )
        await conn.execute(
            "INSERT INTO saved_search (slug, title, query) VALUES ('canary', ?, ?)",
            (C_SAVED_SEARCH, C_SAVED_SEARCH),
        )
        await conn.execute(
            "INSERT INTO briefing_card (slot, icon, title, body) "
            "VALUES ('morning', '*', ?, ?)",
            (C_BRIEFING, C_BRIEFING),
        )
        await conn.execute(
            "INSERT INTO persona_thought "
            "(persona_user_id, chain_id, step_no, kind, seed_kind, text, source_scope) "
            "VALUES (?, 1, 1, 'seed', 'know_you', ?, 'chat')",
            (owner_id, C_THOUGHT),
        )
        await conn.execute(
            "INSERT INTO telegram_person "
            "(persona_user_id, telegram_user_id, username, display_name) "
            "VALUES (?, 777001, ?, ?)",
            (owner_id, C_TELEGRAM, C_TELEGRAM),
        )
        await conn.execute(
            "INSERT INTO clipboard_event (text, length, hash) VALUES (?, 10, 'h1')",
            (C_CLIPBOARD,),
        )
        # Чат владельца (сессия + сообщение).
        cur = await conn.execute(
            "INSERT INTO chat_session "
            "(user_id, title, created_at, updated_at, summary_up_to_id, "
            " auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (owner_id, C_OWNER_CHAT),
        )
        owner_sid = int(cur.lastrowid or 0)
        ids["chat_session"] = owner_sid
        cur = await conn.execute(
            "INSERT INTO chat_message "
            "(session_id, role, content, created_at, is_streaming, is_pinned, access_count) "
            "VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (owner_sid, C_OWNER_CHAT),
        )
        ids["chat_message"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (owner_id, C_OWNER_MEMORY),
        )
        ids["memory"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO skill (user_id, name, content, enabled) VALUES (?, ?, ?, 1)",
            (owner_id, C_OWNER_SKILL, C_OWNER_SKILL),
        )
        ids["skill"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO reflection (user_id, kind, text) VALUES (?, 'insight', ?)",
            (owner_id, C_OWNER_REFLECTION),
        )
        ids["reflection"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO kg_entity (user_id, name, kind) VALUES (?, ?, 'person')",
            (owner_id, C_OWNER_ENTITY),
        )
        ent_a = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO kg_entity (user_id, name, kind) VALUES (?, ?, 'topic')",
            (owner_id, C_OWNER_ENTITY + "-B"),
        )
        ent_b = int(cur.lastrowid or 0)
        await conn.execute(
            "INSERT INTO kg_edge (user_id, from_entity_id, to_entity_id, relation_type) "
            "VALUES (?, ?, ?, 'знает')",
            (owner_id, ent_a, ent_b),
        )
        await conn.commit()
    return ids


async def _seed_global_kv(owner_id: int) -> None:
    """Глобальный ``kv_settings`` владельца: секреты, пути, имена моделей."""
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_id))
        await set_kv(conn, "owner_exclusive_mode", "0")
        await set_kv(conn, "llm_provider", "worker")
        await set_kv(conn, "byo_api_provider", "worker")
        await set_kv(conn, "chat_system_prompt", C_OWNER_PROMPT)
        await set_kv(conn, "telegram_bot_token", C_KV_TG_TOKEN)
        await set_kv(conn, "smtp_password", C_KV_SMTP)
        await set_kv(conn, "brave_api_key", C_KV_BRAVE)
        await set_kv(conn, "mac_fs_roots", C_KV_MACPATH)
        await set_kv(conn, "worker_models", C_KV_WORKERMODEL)
        await set_kv(conn, "ollama_model", C_KV_WORKERMODEL)
        await set_kv(conn, "byo_api_key_ollama", f"http://{C_KV_OLLAMA_URL}:11434")
        await set_kv(conn, "byo_api_key_openai", "sk-owner-secret-key-zz")
        await set_kv(conn, "openai_compatible_base_url", f"https://{C_KV_OLLAMA_URL}/v1")
        await set_kv(conn, "openai_compatible_model", C_KV_WORKERMODEL)
        await set_kv(conn, "train_last_result", C_KV_TRAIN)
        await set_kv(conn, "recall_mode", "generative")
        await set_kv(conn, "ai_everywhere", "1")
        await conn.commit()


async def _seed_member_b(owner_id: int, b_id: int, a_id: int) -> dict[str, int]:
    """Данные ВТОРОГО участника + его переписка с владельцем (не с A!)."""
    ids: dict[str, int] = {}
    async with get_connection() as conn:
        cur = await conn.execute(
            "INSERT INTO chat_session "
            "(user_id, title, created_at, updated_at, summary_up_to_id, "
            " auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (b_id, B_CHAT),
        )
        b_sid = int(cur.lastrowid or 0)
        ids["chat_session"] = b_sid
        cur = await conn.execute(
            "INSERT INTO chat_message "
            "(session_id, role, content, created_at, is_streaming, is_pinned, access_count) "
            "VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (b_sid, B_MESSAGE),
        )
        ids["chat_message"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (b_id, B_MEMORY),
        )
        ids["memory"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO skill (user_id, name, content, enabled) VALUES (?, ?, ?, 1)",
            (b_id, B_SKILL, B_SKILL),
        )
        ids["skill"] = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO reflection (user_id, kind, text) VALUES (?, 'insight', ?)",
            (b_id, B_REFLECTION),
        )
        ids["reflection"] = int(cur.lastrowid or 0)
        await conn.execute(
            "INSERT INTO kg_entity (user_id, name, kind) VALUES (?, ?, 'person')",
            (b_id, B_ENTITY),
        )
        # Дружба B ↔ владелец (A в ней не участвует).
        for x, y in ((b_id, owner_id), (owner_id, b_id)):
            await conn.execute(
                "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)",
                (x, y),
            )
        lo, hi = min(b_id, owner_id), max(b_id, owner_id)
        cur = await conn.execute(
            "INSERT INTO dm_thread (user_a_id, user_b_id, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (lo, hi),
        )
        thread_id = int(cur.lastrowid or 0)
        ids["dm_thread"] = thread_id
        cur = await conn.execute(
            "INSERT INTO dm_message (thread_id, sender_id, body, kind) "
            "VALUES (?, ?, ?, 'human')",
            (thread_id, b_id, B_DM),
        )
        ids["dm_message"] = int(cur.lastrowid or 0)
        await conn.execute(
            "INSERT INTO dm_ai_draft (user_id, thread_id, body) VALUES (?, ?, ?)",
            (b_id, thread_id, B_DRAFT),
        )
        await conn.execute(
            "INSERT INTO social_notif_item (user_id, event, title, body) "
            "VALUES (?, 'dm_message', ?, ?)",
            (b_id, B_NOTIF, B_NOTIF),
        )
        cur = await conn.execute(
            "INSERT INTO llm_grant (grantor_id, grantee_id, daily_limit, note) "
            "VALUES (?, ?, 5, ?)",
            (b_id, owner_id, B_GRANT_NOTE),
        )
        ids["llm_grant"] = int(cur.lastrowid or 0)
        # ЧУЖАЯ заявка (владелец → B): участник A в ней не участвует ни одной
        # стороной, поэтому любой ответ по её id — это уже IDOR.
        cur = await conn.execute(
            "INSERT INTO friend_request (from_user_id, to_user_id, message, status) "
            "VALUES (?, ?, 'чужая заявка', 'pending')",
            (owner_id, b_id),
        )
        ids["friend_request"] = int(cur.lastrowid or 0)
        # Заявка от B к A — легитимная для A (нужна как позитивный контроль).
        cur = await conn.execute(
            "INSERT INTO friend_request (from_user_id, to_user_id, message, status) "
            "VALUES (?, ?, 'привет', 'pending')",
            (b_id, a_id),
        )
        ids["own_friend_request"] = int(cur.lastrowid or 0)
        await set_user_kv(conn, b_id, "chat_system_prompt", B_PROMPT)
        await set_user_kv(conn, b_id, "byo_api_key_openai", B_APIKEY)
        await set_user_kv(conn, b_id, "llm_provider", "openai")
        await set_user_kv(conn, b_id, "social_telegram_token", B_TG_TOKEN)
        await conn.commit()
    return ids


# ── Фикстура ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def env():
    """Реальное приложение + владелец + участник A + участник B."""
    await init_database()
    # Пароли намеренно не содержат локальной части адреса: политика паролей
    # (app/auth/password_policy.py) такие отвергает.
    owner_user = await create_user(C_OWNER_EMAIL, "Zx7-Alpha-Passphrase")
    member_a = await create_user("member-a@iso-audit.test", "Qw4-Bravo-Passphrase")
    member_b = await create_user(B_EMAIL, "Rt9-Delta-Passphrase")
    await _seed_global_kv(owner_user["id"])
    owner_ids = await _seed_owner(owner_user["id"])
    b_ids = await _seed_member_b(owner_user["id"], member_b["id"], member_a["id"])
    setup_gate._cache.mark_done()
    _reset_caches()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "owner": owner_user,
                "a": member_a,
                "b": member_b,
                "owner_ids": owner_ids,
                "b_ids": b_ids,
            }
    finally:
        _reset_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


# ── Перечисление member-поверхности ИЗ ПРИЛОЖЕНИЯ ───────────────────────────


def member_routes() -> list[tuple[str, frozenset[str]]]:
    """Все роуты, попадающие под ``_MEMBER_PREFIXES``. Источник — само приложение.

    Возвращается список ``(path_template, methods)``. Новый роут под member-
    префиксом попадает под аудит АВТОМАТИЧЕСКИ — хардкода нет.
    """
    app = create_app()
    out: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or not _is_member_path(path):
            continue
        methods = {
            m
            for m in (getattr(route, "methods", None) or set())
            if m not in ("HEAD", "OPTIONS")
        }
        out.setdefault(path, set()).update(methods)
    return sorted((p, frozenset(m)) for p, m in out.items())


def member_get_paths() -> list[str]:
    """GET-роуты member-поверхности БЕЗ параметров пути."""
    return sorted(
        p for p, methods in member_routes() if "GET" in methods and "{" not in p
    )


def parameterised_member_routes() -> list[tuple[str, frozenset[str]]]:
    """Роуты member-поверхности С параметрами пути (кандидаты на IDOR)."""
    return [(p, m) for p, m in member_routes() if "{" in p]


_PARAM_RE = re.compile(r"\{([a-z_]+)\}")


def _fill(path: str, values: dict[str, int]) -> str | None:
    """Подставить id в шаблон пути. ``None`` — если нечего подставить."""
    names = _PARAM_RE.findall(path)
    if not names:
        return None
    out = path
    for name in names:
        if name not in values:
            return None
        out = out.replace("{" + name + "}", str(values[name]))
    return out


def leaked(body: str) -> list[str]:
    """Какие канарейки видны в теле — сырьём или в ``\\uXXXX``-экранировании."""
    found: list[str] = []
    for canary in ALL_CANARIES:
        escaped = json.dumps(canary, ensure_ascii=True)[1:-1]
        if canary in body or escaped in body:
            found.append(canary)
    return found


# ── 1. Чеклист поверхности печатается (он же документация аудита) ───────────


def test_member_surface_is_enumerable() -> None:
    """Поверхность берётся из приложения и она непустая.

    Тест намеренно печатает чеклист: при падении соседних тестов видно, какой
    именно набор роутов проверялся.
    """
    routes = member_routes()
    assert len(routes) >= 90, f"member-поверхность подозрительно мала: {len(routes)}"
    for path, methods in routes:
        print(f"{','.join(sorted(methods)):18} {path}")
    assert member_get_paths(), "нет ни одного GET без параметров"


# ── 2. Обход ВСЕХ member-GET без параметров: ни одной канарейки ─────────────


@pytest.mark.asyncio
async def test_member_get_surface_leaks_no_canary(env: dict[str, Any]) -> None:
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])

    leaks: list[str] = []
    served = 0
    for path in member_get_paths():
        response = await client.get(path, follow_redirects=False)
        if response.status_code == 200 and response.text.strip():
            served += 1
        print(f"{response.status_code} {len(response.text):>7}b  {path}")
        blob = response.text + "\n" + "\n".join(
            f"{k}: {v}" for k, v in response.headers.items()
        )
        found = leaked(blob)
        if found:
            leaks.append(f"{path} → {found}")
    assert not leaks, "утечка в member-GET:\n" + "\n".join(leaks)
    # Анти-«зелёная пустышка»: если поверхность вдруг начнёт отдавать 403/редирект
    # на всё подряд, обход выше перестанет что-либо проверять. Порог держим
    # заведомо ниже текущего числа рабочих страниц.
    assert served >= 15, (
        f"участнику реально отрисовалось лишь {served} страниц — обход стал "
        "бессмысленным, проверь гейт"
    )


# ── 3. IDOR: те же роуты, но с ЧУЖИМИ id в пути ────────────────────────────


@pytest.mark.asyncio
async def test_member_get_with_foreign_ids_leaks_nothing(env: dict[str, Any]) -> None:
    """GET-роуты с параметрами, заполненные id ВЛАДЕЛЬЦА и участника B.

    Ожидание: 403/404/303, но НИКОГДА 200 с чужим содержимым.
    """
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])

    owner_ids = env["owner_ids"]
    b_ids = env["b_ids"]
    victims: list[tuple[str, dict[str, int]]] = [
        (
            "владелец",
            {
                "session_id": owner_ids["chat_session"],
                "message_id": owner_ids["chat_message"],
                "mem_id": owner_ids["memory"],
                "skill_id": owner_ids["skill"],
                "ref_id": owner_ids["reflection"],
                "thread_id": b_ids["dm_thread"],
                "user_id": env["owner"]["id"],
                "friend_id": env["owner"]["id"],
                "request_id": b_ids["friend_request"],
                "grant_id": b_ids["llm_grant"],
                "version_id": 1,
            },
        ),
        (
            "участник B",
            {
                "session_id": b_ids["chat_session"],
                "message_id": b_ids["chat_message"],
                "mem_id": b_ids["memory"],
                "skill_id": b_ids["skill"],
                "ref_id": b_ids["reflection"],
                "thread_id": b_ids["dm_thread"],
                "user_id": env["b"]["id"],
                "friend_id": env["b"]["id"],
                "request_id": b_ids["friend_request"],
                "grant_id": b_ids["llm_grant"],
                "version_id": 1,
            },
        ),
    ]

    leaks: list[str] = []
    for label, values in victims:
        for path, methods in parameterised_member_routes():
            if "GET" not in methods:
                continue
            filled = _fill(path, values)
            if filled is None:
                continue
            response = await client.get(filled, follow_redirects=False)
            if response.status_code >= 500:
                leaks.append(f"[{label}] GET {filled} → 500 {response.text[:200]}")
                continue
            found = leaked(response.text)
            if found:
                leaks.append(f"[{label}] GET {filled} → {found}")
    assert not leaks, "IDOR-утечка в GET:\n" + "\n".join(leaks)


# ── 4. POST-фаззинг id-принимающих эндпоинтов чужими id ────────────────────


@pytest.mark.asyncio
async def test_member_post_with_foreign_ids_is_refused(env: dict[str, Any]) -> None:
    """Мутирующие роуты с чужим id: не 2xx и без чужих данных в ответе.

    Исключение — ``/api/chat/sessions/{id}/send*``: они пишут в СВОЮ сессию
    только после ``get_session(uid, id)``; их 404 проверяется тем же правилом.
    """
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])

    owner_ids = env["owner_ids"]
    b_ids = env["b_ids"]
    values = {
        "session_id": owner_ids["chat_session"],
        "message_id": owner_ids["chat_message"],
        "mem_id": owner_ids["memory"],
        "skill_id": owner_ids["skill"],
        "ref_id": owner_ids["reflection"],
        "thread_id": b_ids["dm_thread"],
        "user_id": env["owner"]["id"],
        "friend_id": env["owner"]["id"],
        "request_id": b_ids["friend_request"],
        "grant_id": b_ids["llm_grant"],
        "version_id": 1,
    }
    # Роуты, которым чужой id вообще не адресован (глобальные тумблеры без
    # параметров) сюда не попадают — фильтр по наличию "{" ниже.
    bad: list[str] = []
    for path, methods in parameterised_member_routes():
        filled = _fill(path, values)
        if filled is None:
            continue
        for method in sorted(methods):
            if method == "GET":
                continue
            response = await client.request(
                method,
                filled,
                json={},
                data=None if method in ("DELETE", "PATCH") else None,
                follow_redirects=False,
            )
            status = response.status_code
            found = leaked(response.text)
            if found:
                bad.append(f"{method} {filled} → {status} {found}")
                continue
            if status >= 500:
                bad.append(f"{method} {filled} → 500 {response.text[:200]}")
                continue
            if 200 <= status < 300:
                # 200 допустим ТОЛЬКО как честный отказ: ``ok:false`` либо тело
                # с ``error``. Всё остальное = действие над чужим объектом
                # реально выполнено.
                try:
                    payload = response.json()
                except Exception:  # noqa: BLE001 — HTML-ответ
                    bad.append(f"{method} {filled} → {status} (2xx на чужой id)")
                    continue
                if not (payload.get("ok") is False or "error" in payload):
                    bad.append(f"{method} {filled} → {status} {str(payload)[:160]}")
    assert not bad, "мутация чужого объекта прошла:\n" + "\n".join(bad)

    # Позитивный контроль: СВОЮ заявку участник принимает — фаззер выше не
    # «зелёный» просто потому, что все эти роуты сломаны для всех.
    own = await client.post(f"/api/friends/{b_ids['own_friend_request']}/accept")
    assert own.status_code == 200 and own.json()["ok"] is True, own.text


# ── 5. Владелец не появляется в данных участника (адресные проверки) ───────


@pytest.mark.asyncio
async def test_member_listings_contain_only_own_rows(env: dict[str, Any]) -> None:
    """Листинги участника A: сессии, поиск, память, навыки, граф, друзья."""
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])

    probes = (
        "/api/chat/sessions",
        "/api/chat/memory",
        "/api/skills",
        "/api/graph.json",
        "/api/palette.json",
        "/api/llm/models",
        "/api/account.json",
        "/api/messages/unread.json",
        "/api/social-notif/pending",
        "/api/chat/search?q=KANAREYKA",
        "/api/settings/search?q=KANAREYKA",
        "/api/friends/search?q=KANAREYKA",
        "/api/copilot/ask?q=KANAREYKA",
        "/settings/memory",
        "/settings/skills",
        "/settings/llm",
        "/settings/llm/sharing",
        "/settings/system-prompt",
        "/settings/notifications-social",
        "/friends",
        "/messages",
        "/graph",
        "/chat",
    )
    leaks: list[str] = []
    for path in probes:
        response = await client.get(path, follow_redirects=False)
        found = leaked(response.text)
        if found:
            leaks.append(f"{path} → {found}")
    assert not leaks, "чужие данные в листинге участника:\n" + "\n".join(leaks)


@pytest.mark.asyncio
async def test_member_b_data_never_reaches_member_a(env: dict[str, Any]) -> None:
    """Прямые обращения A к объектам B: чат, сообщения, ветка, черновик."""
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])
    b_ids = env["b_ids"]

    checks: list[tuple[str, str]] = [
        ("GET", f"/api/chat/sessions/{b_ids['chat_session']}/messages"),
        ("GET", f"/api/chat/sessions/{b_ids['chat_session']}/live"),
        ("GET", f"/api/chat/activity/{b_ids['chat_session']}"),
        ("GET", f"/chat/{b_ids['chat_session']}"),
        ("GET", f"/api/messages/{b_ids['dm_thread']}/poll"),
        ("GET", f"/api/messages/{b_ids['dm_thread']}/older"),
        ("GET", f"/api/messages/{b_ids['dm_thread']}/ai"),
        ("GET", f"/messages/{b_ids['dm_thread']}"),
        ("GET", f"/messages/with/{env['b']['id']}"),
    ]
    problems: list[str] = []
    for method, path in checks:
        response = await client.request(method, path, follow_redirects=False)
        found = leaked(response.text)
        if found:
            problems.append(f"{method} {path} → {found}")
        elif 200 <= response.status_code < 300 and path.startswith("/api/"):
            # 200 на чужой объект — уже подозрение, даже если тело пустое.
            payload = response.json()
            if payload.get("error") is None and payload not in ({}, {"streaming": False}):
                problems.append(f"{method} {path} → 200 {str(payload)[:160]}")
    assert not problems, "данные участника B видны участнику A:\n" + "\n".join(problems)


@pytest.mark.asyncio
async def test_member_cannot_mutate_member_b_objects(env: dict[str, Any]) -> None:
    """A не может править/удалять объекты B и не видит их после попытки."""
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])
    b_ids = env["b_ids"]

    await client.post(f"/settings/memory/{b_ids['memory']}/delete", follow_redirects=False)
    await client.post(
        f"/settings/memory/{b_ids['memory']}/edit",
        data={"text": "переписано чужим"},
        follow_redirects=False,
    )
    await client.post(f"/settings/skills/{b_ids['skill']}/delete", follow_redirects=False)
    await client.post(
        f"/settings/skills/{b_ids['skill']}/toggle", data={}, follow_redirects=False
    )
    await client.request("DELETE", f"/api/chat/sessions/{b_ids['chat_session']}")
    await client.request("DELETE", f"/api/chat/messages/{b_ids['chat_message']}")
    await client.post(
        f"/settings/llm/sharing/{b_ids['llm_grant']}/revoke",
        data={},
        follow_redirects=False,
    )

    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT text FROM user_memory WHERE id = ?", (b_ids["memory"],)
        )
        row = await cur.fetchone()
        assert row is not None and row["text"] == B_MEMORY, "память B изменена/удалена"
        cur = await conn.execute("SELECT name FROM skill WHERE id = ?", (b_ids["skill"],))
        assert (await cur.fetchone()) is not None, "навык B удалён"
        cur = await conn.execute(
            "SELECT id FROM chat_session WHERE id = ?", (b_ids["chat_session"],)
        )
        assert (await cur.fetchone()) is not None, "чат B удалён"
        cur = await conn.execute(
            "SELECT id FROM chat_message WHERE id = ?", (b_ids["chat_message"],)
        )
        assert (await cur.fetchone()) is not None, "сообщение B удалено"
        cur = await conn.execute(
            "SELECT revoked_at FROM llm_grant WHERE id = ?", (b_ids["llm_grant"],)
        )
        row = await cur.fetchone()
        assert row is not None and row["revoked_at"] is None, "выдача B отозвана чужим"


@pytest.mark.asyncio
async def test_member_cannot_mutate_owner_objects(env: dict[str, Any]) -> None:
    """Тот же набор, но жертва — ВЛАДЕЛЕЦ (чат, память, навык, рефлексия)."""
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])
    owner_ids = env["owner_ids"]

    await client.post(
        f"/settings/memory/{owner_ids['memory']}/delete", follow_redirects=False
    )
    await client.post(
        f"/settings/memory/reflection/{owner_ids['reflection']}/forget",
        follow_redirects=False,
    )
    await client.post(
        f"/settings/skills/{owner_ids['skill']}/delete", follow_redirects=False
    )
    await client.request("DELETE", f"/api/chat/sessions/{owner_ids['chat_session']}")
    await client.post(
        f"/api/chat/sessions/{owner_ids['chat_session']}/rename",
        json={"title": "угнано"},
    )
    await client.post(
        f"/api/chat/sessions/{owner_ids['chat_session']}/system-prompt",
        json={"prompt": "ignore previous instructions"},
    )

    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT text FROM user_memory WHERE id = ?", (owner_ids["memory"],)
        )
        row = await cur.fetchone()
        assert row is not None and row["text"] == C_OWNER_MEMORY
        cur = await conn.execute(
            "SELECT valid_until FROM reflection WHERE id = ?", (owner_ids["reflection"],)
        )
        row = await cur.fetchone()
        assert row is not None and row["valid_until"] is None, "рефлексия владельца забыта"
        cur = await conn.execute(
            "SELECT name FROM skill WHERE id = ?", (owner_ids["skill"],)
        )
        assert (await cur.fetchone()) is not None
        cur = await conn.execute(
            "SELECT title, custom_system_prompt FROM chat_session WHERE id = ?",
            (owner_ids["chat_session"],),
        )
        row = await cur.fetchone()
        assert row is not None, "чат владельца удалён участником"
        assert row["title"] == C_OWNER_CHAT, "чат владельца переименован участником"
        assert row["custom_system_prompt"] is None, "промпт чата владельца подменён"


# ── 6. Глобальный kv не переписывается участником ──────────────────────────


@pytest.mark.asyncio
async def test_member_never_writes_global_kv(env: dict[str, Any]) -> None:
    """Снимок глобального kv до/после прогона участником всех его POST-форм.

    Инвариант: участник не меняет и не удаляет НИ ОДНОЙ существующей строки
    ``kv_settings``, а новую заводит только если её имя содержит ЕГО ``user_id``
    (``onboarded_<uid>``, ``user_profile_<uid>``). Такой ключ по построению
    недостижим ни для владельца, ни для другого участника.
    """
    client: AsyncClient = env["client"]
    uid = env["a"]["id"]

    async def snapshot() -> dict[str, str]:
        async with get_connection() as conn:
            cur = await conn.execute("SELECT key, value FROM kv_settings")
            return {str(r["key"]): str(r["value"]) for r in await cur.fetchall()}

    before = await snapshot()
    await _as(client, uid)

    await client.post("/settings/theme", data={"theme": "light"}, follow_redirects=False)
    await client.post(
        "/api/settings/ui-language", data={"language": "en"}, follow_redirects=False
    )
    await client.post(
        "/settings/system-prompt",
        data={"prompt_text": "мой собственный характер"},
        follow_redirects=False,
    )
    await client.post(
        "/settings/advanced",
        data={"master": "1", "recall_mode": "generative", "tools": "1"},
        follow_redirects=False,
    )
    await client.post(
        "/settings/advanced/profile", data={"profile": "full"}, follow_redirects=False
    )
    await client.post(
        "/settings/memory/engine",
        data={"dream_enabled": "1", "dream_hour_local": "9", "recall_mode": "vector"},
        follow_redirects=False,
    )
    await client.post("/settings/memory/train", follow_redirects=False)
    await client.post(
        "/settings/llm",
        data={"provider": "openai", "openai_api_key": "sk-member-own"},
        follow_redirects=False,
    )
    await client.post(
        "/settings/system-prompt/history/toggle",
        data={"enabled": "1"},
        follow_redirects=False,
    )
    await client.post("/api/chat/auto-prompt", json={"on": True})
    await client.post("/onboarding/complete", follow_redirects=False)
    await client.post(
        "/settings/profile",
        data={"profile_text": "я участник A"},
        follow_redirects=False,
    )
    await client.post(
        "/settings/notifications-social",
        data={"dm_message__telegram": "on", "tg_token": "222:member", "tg_chat_id": "42"},
        follow_redirects=False,
    )

    after = await snapshot()
    overwritten = {
        key: (before[key], value)
        for key, value in after.items()
        if key in before and before[key] != value
    }
    added = sorted(set(after) - set(before))
    not_own = [key for key in added if not key.endswith(f"_{uid}")]
    removed = sorted(set(before) - set(after))
    assert not overwritten, f"участник переписал глобальный kv: {overwritten}"
    assert not removed, f"участник удалил строки глобального kv: {removed}"
    assert not not_own, (
        f"участник завёл строки глобального kv без своего user_id: {not_own}"
    )


@pytest.mark.asyncio
async def test_member_session_kv_keys_are_scoped_to_own_sessions(
    env: dict[str, Any],
) -> None:
    """Настройки сессии живут в ГЛОБАЛЬНОМ kv (``chat_mode_<id>`` и т.п.).

    Это осознанный компромисс: ключ содержит id сессии, а сама сессия
    резолвится по ``get_session(uid, id)``. Тест сторожит ровно инвариант —
    участник заводит такие строки ТОЛЬКО для своих сессий, а строки владельца
    остаются нетронутыми.
    """
    client: AsyncClient = env["client"]
    await _as(client, env["a"]["id"])
    owner_sid = env["owner_ids"]["chat_session"]

    created = await client.post("/api/chat/sessions", json={"title": "A"})
    own_sid = created.json()["id"]

    assert (
        await client.post(f"/api/chat/sessions/{own_sid}/mode", json={"mode": "plan"})
    ).status_code == 200
    assert (
        await client.post(f"/api/chat/sessions/{owner_sid}/mode", json={"mode": "bypass"})
    ).status_code == 404
    assert (
        await client.post(f"/api/chat/sessions/{owner_sid}/effort", json={"effort": "deep"})
    ).status_code == 404
    assert (
        await client.post(f"/api/chat/sessions/{owner_sid}/stop")
    ).status_code == 404

    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT key FROM kv_settings WHERE key LIKE 'chat\\_%' ESCAPE '\\'"
        )
        keys = {str(r["key"]) for r in await cur.fetchall()}
    assert f"chat_mode_{own_sid}" in keys, "своя сессия перестала настраиваться"
    for prefix in ("chat_mode", "chat_effort", "chat_stop"):
        assert f"{prefix}_{owner_sid}" not in keys, (
            f"участник завёл {prefix}_{owner_sid} для сессии ВЛАДЕЛЬЦА"
        )


# ── 7. Контроль: владелец ничего не потерял ────────────────────────────────


@pytest.mark.asyncio
async def test_owner_still_sees_own_data(env: dict[str, Any]) -> None:
    """Без этого «зелень» можно было бы получить, сломав фичу для всех."""
    client: AsyncClient = env["client"]
    await _as(client, env["owner"]["id"])

    graph = await client.get("/api/graph.json")
    assert graph.status_code == 200, graph.text
    body = graph.text
    assert C_HOURLY in body or json.dumps(C_HOURLY)[1:-1] in body, (
        "владелец перестал видеть свои часовые карточки в графе"
    )

    palette = await client.get("/api/palette.json")
    assert palette.status_code == 200
    assert C_TAG in palette.text, "теги владельца пропали из палитры"
    assert C_SAVED_SEARCH in palette.text, "сохранённые поиски пропали из палитры"

    sessions = await client.get("/api/chat/sessions")
    assert C_OWNER_CHAT in sessions.text, "чат владельца пропал из его списка"

    prompt_page = await client.get("/settings/system-prompt")
    assert prompt_page.status_code == 200
    assert (
        C_OWNER_PROMPT in prompt_page.text
        or json.dumps(C_OWNER_PROMPT)[1:-1] in prompt_page.text
    ), "владелец перестал видеть свой системный промпт"

    memory_page = await client.get("/settings/memory")
    assert memory_page.status_code == 200
    assert C_OWNER_MEMORY in memory_page.text, "память владельца пропала"
    assert C_KV_TRAIN in memory_page.text, "результат прогона владельца пропал"

    models = await client.get("/api/llm/models")
    assert models.status_code == 200
    assert C_KV_WORKERMODEL in models.text, "модели воркера пропали у владельца"


@pytest.mark.asyncio
async def test_owner_can_still_mutate_own_objects(env: dict[str, Any]) -> None:
    """Владелец по-прежнему правит свои сессии/память/движок памяти."""
    client: AsyncClient = env["client"]
    await _as(client, env["owner"]["id"])
    owner_ids = env["owner_ids"]

    renamed = await client.post(
        f"/api/chat/sessions/{owner_ids['chat_session']}/rename",
        json={"title": "переименовано владельцем"},
    )
    assert renamed.status_code == 200, renamed.text

    engine = await client.post(
        "/settings/memory/engine",
        data={"dream_enabled": "1", "dream_hour_local": "5"},
        follow_redirects=False,
    )
    assert engine.status_code == 303, engine.text

    mode = await client.post(
        f"/api/chat/sessions/{owner_ids['chat_session']}/mode", json={"mode": "plan"}
    )
    assert mode.status_code == 200, mode.text


# ── 7b. Дружба не раскрывает e-mail (единая политика маскирования) ─────────


@pytest.mark.asyncio
async def test_friendship_never_reveals_raw_email(env: dict[str, Any]) -> None:
    """Участник подружился с ВЛАДЕЛЬЦЕМ — его адрес всё равно только под маской.

    ``/friends``, ``/messages`` и поиск людей маскируют адрес намеренно
    (:func:`app.social.repository._mask_email`), а ``/settings/llm/sharing``
    печатал его целиком: список друзей уезжал в ``<datalist>`` сырыми
    адресами. То есть достаточно было подружиться, чтобы забрать настоящую
    почту человека — включая почту владельца инстанса.
    """
    client: AsyncClient = env["client"]
    a_id, owner_id = env["a"]["id"], env["owner"]["id"]
    async with get_connection() as conn:
        for x, y in ((a_id, owner_id), (owner_id, a_id)):
            await conn.execute(
                "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)",
                (x, y),
            )
        await conn.commit()
    await _as(client, a_id)

    for path in (
        "/friends",
        "/messages",
        "/settings/llm/sharing",
        f"/api/friends/search?q={C_OWNER_EMAIL}",
    ):
        response = await client.get(path, follow_redirects=False)
        assert C_OWNER_EMAIL not in response.text, (
            f"{path} показал участнику полный e-mail владельца"
        )

    # Страница выбора называет друга маской, а выбирает его по id — адреса в
    # потоке нет вообще. Фича при этом жива: выдача проходит.
    page = await client.get("/settings/llm/sharing")
    assert "z***@iso-audit.test" in page.text, "друг пропал из списка выбора"
    assert f'value="{owner_id}"' in page.text, "в форме нет id друга"

    granted = await client.post(
        "/settings/llm/sharing/grant",
        data={"friend_id": str(owner_id), "daily_limit": "5"},
        follow_redirects=False,
    )
    assert granted.status_code == 200, granted.text
    assert C_OWNER_EMAIL not in granted.text, "подтверждение выдачи печатает адрес"
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT grantee_id FROM llm_grant WHERE grantor_id = ?", (a_id,)
        )
        row = await cur.fetchone()
    assert row is not None and int(row["grantee_id"]) == owner_id, (
        "выдача по id друга не сработала — форму сломали, а не починили"
    )

    # …а по id НЕ-друга — нет: id в форме клиентский, и без резолва по своим
    # друзьям это была бы раздача доступа перебором номеров.
    stranger = await client.post(
        "/settings/llm/sharing/grant",
        data={"friend_id": str(env["b"]["id"]), "daily_limit": "5"},
        follow_redirects=False,
    )
    assert stranger.status_code == 400, stranger.text
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM llm_grant WHERE grantor_id = ? AND grantee_id = ?",
            (a_id, env["b"]["id"]),
        )
        assert int((await cur.fetchone())["n"]) == 0, "выдача чужому по id прошла"


@pytest.mark.asyncio
async def test_borrowed_model_badge_names_the_grantor_without_the_address(
    env: dict[str, Any],
) -> None:
    """Одолжили модель → бейдж чата называет выдавшего, но не его почту.

    ``borrowed_status`` уезжает в контекст шаблона чата целиком
    (``_provider_badge`` → ``badge["borrowed"]``), и раньше несла сырой
    ``grantor_email`` выдавшего — то есть принять чужой доступ значило
    получить его настоящий адрес в payload'е собственной страницы.
    """
    from app.llm.grants import borrowed_status

    client: AsyncClient = env["client"]
    a_id, owner_id = env["a"]["id"], env["owner"]["id"]
    async with get_connection() as conn:
        for x, y in ((a_id, owner_id), (owner_id, a_id)):
            await conn.execute(
                "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)",
                (x, y),
            )
        await conn.execute(
            "INSERT INTO llm_grant (grantor_id, grantee_id, daily_limit) "
            "VALUES (?, ?, 10)",
            (owner_id, a_id),
        )
        await conn.commit()

    status = await borrowed_status(a_id)
    assert status is not None, "выдача не подхватилась — тест ничего не сторожит"
    assert "grantor_email" not in status
    assert status["grantor_name"] == "z***@iso-audit.test"
    assert C_OWNER_EMAIL not in json.dumps(status, ensure_ascii=False)

    await _as(client, a_id)
    page = await client.get("/chat", follow_redirects=False)
    assert C_OWNER_EMAIL not in page.text, "адрес выдавшего попал в разметку чата"


# ── 7c. /build — единственный инструментальный путь участника ──────────────
#
# В остальном чате инструменты выключены не-владельцу (`_tools_on`), а этот
# роут пишет файлы напрямую через ``call_tool("write_file")``. Проверяем, что
# запись физически не может уехать за пределы пространства вызывающего.


class _FakeOllama:
    """Подменяет OllamaClient: возвращает файлы, в сеть не ходит."""

    def __init__(self, files: list[dict[str, str]]) -> None:
        self._files = files
        self.calls = 0

    async def complete_json(self, _request: Any, _schema: Any) -> dict[str, Any]:
        self.calls += 1
        return {"files": self._files}


def _install_fake_ollama(
    monkeypatch: pytest.MonkeyPatch, files: list[dict[str, str]]
) -> _FakeOllama:
    """Подсунуть фейковый клиент в тот же символ, что импортирует роут."""
    from app.llm import client as llm_client

    fake = _FakeOllama(files)
    monkeypatch.setattr(llm_client, "OllamaClient", lambda **_kw: fake)
    return fake


#: Пути, которыми модель могла бы попробовать выйти из чужого workspace.
_ESCAPE_PATHS: tuple[str, ...] = (
    "../../owner-secret.txt",
    "/etc/persona-owned.txt",
    "..\\..\\owner-secret.txt",
)


@pytest.mark.asyncio
async def test_member_build_writes_only_into_own_workspace(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Файлы участника ложатся ТОЛЬКО в его каталог; побеги отброшены.

    ``data_dir`` изолирован на тест (autouse-фикстура в conftest), поэтому
    «в каталоге владельца пусто» — честная проверка, а не остаток от соседа.
    """
    from app.workspace.dirs import ensure_user_workspace, workspace_root

    client: AsyncClient = env["client"]
    a_id, owner_id = env["a"]["id"], env["owner"]["id"]
    async with get_connection() as conn:
        await set_user_kv(conn, a_id, "byo_api_key_ollama", "http://127.0.0.1:1/")
        await conn.commit()
    await _as(client, a_id)

    fake = _install_fake_ollama(
        monkeypatch,
        [{"path": "index.html", "content": "<h1>мой</h1>"}]
        + [{"path": p, "content": "УГНАНО"} for p in _ESCAPE_PATHS],
    )

    created = await client.post("/api/chat/sessions", json={"title": "A"})
    sid = created.json()["id"]
    response = await client.post(
        f"/api/chat/sessions/{sid}/build", json={"prompt": "сделай страницу"}
    )
    assert response.status_code == 200, response.text
    assert fake.calls == 1

    mine = ensure_user_workspace(a_id)
    assert (mine / "index.html").exists(), "свой файл не записался — фича сломана"
    # Ничего не появилось ни у владельца, ни выше корня всех workspace'ов.
    owner_ws = ensure_user_workspace(owner_id)
    assert not any(owner_ws.rglob("*")), "участник записал файл в workspace владельца"
    assert not (workspace_root().parent / "owner-secret.txt").exists()
    for name in ("owner-secret.txt", "persona-owned.txt"):
        assert not (workspace_root() / name).exists(), name


@pytest.mark.asyncio
async def test_member_build_refused_when_writes_route_to_a_device(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """mac-режим ВКЛ, своего устройства нет → 403 и НИ ОДНОЙ команды в очередь.

    Раньше отказ висел на неявном инварианте двумя модулями ниже: ``run_remote``
    случайно не находил устройство и возвращал строку с ошибкой. Теперь это
    решение принято явно и ДО генерации.
    """
    client: AsyncClient = env["client"]
    a_id = env["a"]["id"]
    async with get_connection() as conn:
        await set_kv(conn, "mac_fs_enabled", "1")
        await set_user_kv(conn, a_id, "byo_api_key_ollama", "http://127.0.0.1:1/")
        await conn.commit()
    await _as(client, a_id)

    fake = _install_fake_ollama(monkeypatch, [{"path": "a.txt", "content": "x"}])
    created = await client.post("/api/chat/sessions", json={"title": "A"})
    sid = created.json()["id"]
    response = await client.post(
        f"/api/chat/sessions/{sid}/build", json={"prompt": "сделай файл"}
    )
    assert response.status_code == 403, response.text
    assert fake.calls == 0, "чужую модель дёрнули, чтобы потом отказать"

    async with get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM agent_fs_command")
        assert int((await cur.fetchone())["n"]) == 0, (
            "участник поставил команду записи в очередь устройства"
        )


@pytest.mark.asyncio
async def test_build_scope_gate_is_fail_closed_for_member_only(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой резолва адреса записи: участнику отказ, владельцу — как раньше."""
    from app.devices import fs_rpc
    from app.web.routes.chat_sessions import _build_write_refusal

    async def _boom() -> bool:
        raise RuntimeError("БД занята")

    monkeypatch.setattr(fs_rpc, "is_enabled", _boom)
    assert await _build_write_refusal(env["a"]["id"], owner=False) is not None
    assert await _build_write_refusal(env["owner"]["id"], owner=True) is None


@pytest.mark.asyncio
async def test_owner_build_unaffected_by_the_gate(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль: у владельца сборка работает и при ВКЛ mac-режиме без устройства.

    Это и есть «поведение владельца не меняется»: гейт для него не выносит
    вердикта вовсе, а куда уедет файл — решает существующий ``call_tool``.
    """
    from app.web.routes.chat_sessions import _build_write_refusal
    from app.workspace.dirs import ensure_user_workspace

    client: AsyncClient = env["client"]
    owner_id = env["owner"]["id"]
    async with get_connection() as conn:
        await set_kv(conn, "mac_fs_enabled", "1")
        await conn.commit()
    assert await _build_write_refusal(owner_id, owner=True) is None

    # …и сквозной прогон при выключенном mac-режиме — файл владельца на месте.
    async with get_connection() as conn:
        await set_kv(conn, "mac_fs_enabled", "0")
        await conn.commit()
    await _as(client, owner_id)
    fake = _install_fake_ollama(
        monkeypatch, [{"path": "owner.html", "content": "<h1>владелец</h1>"}]
    )
    created = await client.post("/api/chat/sessions", json={"title": "владелец"})
    sid = created.json()["id"]
    response = await client.post(
        f"/api/chat/sessions/{sid}/build", json={"prompt": "сделай страницу"}
    )
    assert response.status_code == 200, response.text
    assert fake.calls == 1, "у владельца сборка перестала звать модель"
    assert (ensure_user_workspace(owner_id) / "owner.html").exists()


# ── 8. Fail-closed: сбой резолва роли не выдаёт данные владельца ───────────


@pytest.mark.asyncio
async def test_owner_resolution_failure_degrades_to_member(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Если ``is_owner`` бросает — ВЛАДЕЛЕЦ видит урезанную выдачу, но НЕ 500.

    Проверяем именно направление отказа: сломанный резолв обязан схлопывать
    роль в «участника», а не наоборот. Тест гоняет запросы ВЛАДЕЛЬЦА — если
    fail-closed работает, ГЛОБАЛЬНЫЕ данные инстанса (часовые карточки, теги,
    сохранённые поиски, модели ПК-воркера) в ответе НЕ появятся. Его личные
    строки (чаты, сущности графа) остаются: они и так фильтруются по user_id,
    роль на них не влияет.
    """
    from app.web.routes import owner_view

    client: AsyncClient = env["client"]
    await _as(client, env["owner"]["id"])

    async def _boom(_user_id: int | None) -> bool:
        raise RuntimeError("резолв роли недоступен")

    monkeypatch.setattr(owner_view, "_is_owner", _boom)

    global_only = (C_HOURLY, C_TRANSCRIPT, C_TAG, C_SAVED_SEARCH, C_KV_WORKERMODEL)
    for path in ("/api/graph.json", "/api/palette.json", "/api/llm/models"):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code == 200, f"{path} → {response.status_code}"
        found = [c for c in global_only if c in response.text]
        assert not found, f"{path} отдал глобальные данные при сбое резолва: {found}"


@pytest.mark.asyncio
async def test_discoverable_default_hides_on_resolver_failure(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой резолва роли не выставляет владельца в чужой поиск людей."""
    from app.social import repository as social_repo

    async def _boom(_user_id: int | None) -> bool:
        raise RuntimeError("резолв роли недоступен")

    monkeypatch.setattr(social_repo, "is_owner", _boom)
    assert await social_repo._default_discoverable(env["owner"]["id"]) == "0"
    assert await social_repo.is_discoverable(env["owner"]["id"]) is False


# ── 9. Промпт участника не несёт данных владельца (send-stream) ────────────


class _RecordingClient:
    """Фейковый LLM-клиент: запоминает системный промпт, в сеть не ходит."""

    provider = "openai"

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink
        self.last_input_tokens = None
        self.last_output_tokens = None

    async def complete(self, request: Any) -> str:
        self._sink.append(request.system or "")
        return "ок"

    async def stream(self, request: Any):
        self._sink.append(request.system or "")
        yield "ок"


@pytest.mark.asyncio
async def test_member_prompt_carries_no_foreign_data(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Системный промпт участника: ни захвата владельца, ни данных участника B."""
    from app.web.routes import chat_sessions as chat_routes

    client: AsyncClient = env["client"]
    uid = env["a"]["id"]
    async with get_connection() as conn:
        await set_user_kv(conn, uid, "llm_provider", "openai")
        await set_user_kv(conn, uid, "byo_api_key_openai", "sk-member-a-own")
        await conn.commit()
    await _as(client, uid)

    prompts: list[str] = []
    monkeypatch.setattr(
        chat_routes, "make_client", lambda *a, **kw: _RecordingClient(prompts)
    )

    created = await client.post("/api/chat/sessions", json={"title": "A"})
    assert created.status_code in (200, 201), created.text
    sid = created.json()["id"]

    for question in (
        "что я делал сегодня на компьютере?",
        "напомни, что мы обсуждали про KANAREYKA",
        "кто такой KANAREYKA-ENTITY-OWNER-ZW21?",
    ):
        response = await client.post(
            f"/api/chat/sessions/{sid}/send-stream", json={"question": question}
        )
        assert response.status_code == 200, response.text

    assert prompts, "промпт не собрался — тест ничего не проверил"
    found = leaked("\n".join(prompts))
    assert not found, f"в промпт участника попали чужие данные: {found}"
