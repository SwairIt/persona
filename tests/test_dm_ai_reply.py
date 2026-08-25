"""ИИ отвечает в личных сообщениях: режимы, ограничители и граница контекста.

Что здесь закрепляется (менять только вместе с ``app/social/ai_reply.py``,
``app/social/ai_pref.py`` и миграцией 231):

* по умолчанию НИЧЕГО не происходит — ``off`` во всех переписках;
* ``draft`` рождает черновик, который виден ТОЛЬКО его владельцу и НЕ
  является сообщением (в ``dm_message`` его нет, poll собеседника пуст);
* ``auto`` пишет с ``kind='ai'`` и меткой «✨ ответил ИИ» для ОБОИХ;
* ИИ НИКОГДА не отвечает на сообщение ИИ (иначе два ассистента говорят
  друг с другом до упора квоты);
* дневная квота и минимальный интервал деградируют ``auto`` → ``draft``,
  а не в молчание;
* ``auto`` без явного согласия сохраняется как ``draft``;
* не настроенная модель — не 500 и не сообщение, а подсказка в UI;
* в промпт НЕ попадает ничего из личных данных: чат-сессии, ``user_memory``,
  захват экрана/звука, заметки, напоминания, характер ВЛАДЕЛЬЦА инстанса.

Все вызовы модели замоканы — ни один тест не ходит в сеть. Время всегда
передаётся явно: ни одного ``sleep``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.llm.client import LLMNotConfigured
from app.social import ai_pref, ai_reply
from app.social import repository as social
from app.storage.db import get_connection
from app.storage.repository import set_kv, set_user_kv
from app.web import rate_limit, templates_engine
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware

# ── Канарейки: личные данные, которых в промпте быть НЕ ДОЛЖНО ─────────────
CANARY_CHAT = "КАНАРЕЙКА-ЛИЧНЫЙ-ЧАТ-DM01"
CANARY_MEMORY = "КАНАРЕЙКА-ПАМЯТЬ-О-ЧЕЛОВЕКЕ-DM02"
CANARY_SHOT = "КАНАРЕЙКА-СКРИНШОТ-DM03"
CANARY_AUDIO = "КАНАРЕЙКА-МИКРОФОН-DM04"
CANARY_NOTE = "КАНАРЕЙКА-ЗАМЕТКА-DM05"
CANARY_REMINDER = "КАНАРЕЙКА-НАПОМИНАНИЕ-DM06"
CANARY_OWNER_PROMPT = "КАНАРЕЙКА-ХАРАКТЕР-ВЛАДЕЛЬЦА-DM07"

ALL_CANARIES = (
    CANARY_CHAT,
    CANARY_MEMORY,
    CANARY_SHOT,
    CANARY_AUDIO,
    CANARY_NOTE,
    CANARY_REMINDER,
    CANARY_OWNER_PROMPT,
)

T0 = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _reset_caches() -> None:
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0
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
    rate_limit._EVENTS.clear()


def _app():
    from fastapi import FastAPI

    from app.web.routes import dm_ai, friends, messages

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(friends.router)
    app.include_router(messages.router)
    app.include_router(dm_ai.router)
    return app


class FakeLLM:
    """Подставной клиент: запоминает КАЖДЫЙ запрос и от чьего имени он ушёл."""

    def __init__(self) -> None:
        self.requests: list[object] = []
        self.calls: list[tuple[str, int | None]] = []
        self.reply = "Привет! Спрошу у него и вернусь."
        self.error: Exception | None = None

    def factory(self, *, kind: str = "unknown", user_id: int | None = None, **_kw):
        self.calls.append((kind, user_id))
        if self.error is not None:
            raise self.error
        outer = self

        class _Client:
            provider = "fake"

            async def complete(self, request):
                outer.requests.append(request)
                return outer.reply

        return _Client()

    @property
    def last_prompt(self) -> str:
        request = self.requests[-1]
        return f"{request.system}\n{request.user}"  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def env(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch):
    """Владелец + Аня и Боря (друзья, есть ветка) + подставной LLM."""
    owner_user = await create_user("owner@dmai.test", "Zq7-frost-lantern-91", "Владелец")
    anya = await create_user("anya@dmai.test", "Kp4-velvet-harbour-38", "Аня")
    borya = await create_user("borya@dmai.test", "Kp4-velvet-harbour-38", "Боря")
    vika = await create_user("vika@dmai.test", "Kp4-velvet-harbour-38", "Вика")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    _reset_caches()

    request_id = await social.send_request(anya["id"], borya["id"])
    assert await social.accept_request(request_id, borya["id"])
    thread_id = await social.get_or_create_thread(anya["id"], borya["id"])

    fake = FakeLLM()
    monkeypatch.setattr(ai_reply, "make_client", fake.factory)

    transport = ASGITransport(app=_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, thread_id, anya, borya, vika, owner_user, fake
    finally:
        _reset_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


async def _drain_background() -> None:
    """Дождаться fire-and-forget задачи, которую пускает POST /send."""
    for _ in range(50):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and (task.get_name() or "").startswith("dm-ai-reply-")
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=5)


async def _incoming(thread_id: int, sender_id: int, body: str) -> int:
    """Собеседник написал — возвращаем id сообщения-триггера."""
    message = await social.send_message(thread_id, sender_id, body)
    return int(message["id"])


async def _count_messages(thread_id: int) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM dm_message WHERE thread_id = ?", (thread_id,)
        )
        row = await cursor.fetchone()
    return int(row["n"])


# ── A. Дефолт: выключено ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_mode_is_off_and_generates_nothing(env) -> None:
    _client, _db, thread_id, _anya, borya, _vika, _owner, fake = env
    message_id = await _incoming(thread_id, borya["id"], "привет, ты тут?")

    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    assert outcome["action"] == "none"
    assert outcome["reason"] == "mode_off"
    # Ни одного обращения к модели: выключено — значит вообще ничего.
    assert fake.calls == []
    assert await _count_messages(thread_id) == 1


@pytest.mark.asyncio
async def test_pref_is_per_user_not_per_thread(env) -> None:
    """Режим Ани не включает ИИ Боре: настройка принадлежит ЧЕЛОВЕКУ."""
    _client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    await ai_pref.save_pref(anya["id"], borya["id"], mode="draft")

    # Пишет АНЯ → отвечать должен был бы Борин ИИ, а он выключен.
    message_id = await _incoming(thread_id, anya["id"], "и тебе привет")
    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    assert outcome["action"] == "none"
    assert fake.calls == []
    assert (await ai_pref.get_pref(borya["id"], anya["id"]))["mode"] == "off"


# ── B. Черновик: виден ТОЛЬКО владельцу ─────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_is_produced_and_is_not_a_message(env) -> None:
    _client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    await ai_pref.save_pref(anya["id"], borya["id"], mode="draft")
    message_id = await _incoming(thread_id, borya["id"], "встретимся завтра?")

    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    assert outcome["action"] == "draft"
    assert fake.calls == [("dm_reply", anya["id"])]  # платит тот, кто включил
    # Черновик — НЕ сообщение: в ветке по-прежнему одно.
    assert await _count_messages(thread_id) == 1

    mine = await ai_pref.get_draft(anya["id"], thread_id)
    assert mine is not None
    assert mine["body"] == fake.reply
    # У собеседника черновика нет вовсе — ключ (user_id, thread_id) чужой.
    assert await ai_pref.get_draft(borya["id"], thread_id) is None


@pytest.mark.asyncio
async def test_draft_is_invisible_to_the_peer_over_http(env) -> None:
    client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    fake.reply = "СЕКРЕТНЫЙ-ЧЕРНОВИК-ТОЛЬКО-ДЛЯ-АНИ"
    await ai_pref.save_pref(anya["id"], borya["id"], mode="draft")
    message_id = await _incoming(thread_id, borya["id"], "ты как?")
    await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    # Владелец черновика — видит.
    await _as(client, anya["id"])
    mine = await client.get(f"/api/messages/{thread_id}/ai")
    assert mine.status_code == 200
    assert mine.json()["draft"]["body"] == fake.reply

    # Собеседник — НЕ видит ни в своей панели, ни в poll, ни на странице.
    await _as(client, borya["id"])
    theirs = await client.get(f"/api/messages/{thread_id}/ai")
    assert theirs.status_code == 200
    assert theirs.json()["draft"] is None
    assert fake.reply not in theirs.text

    polled = await client.get(f"/api/messages/{thread_id}/poll", params={"after_id": 0})
    assert fake.reply not in polled.text
    page = await client.get(f"/messages/{thread_id}")
    assert fake.reply not in page.text


@pytest.mark.asyncio
async def test_dismiss_removes_the_draft(env) -> None:
    client, _db, thread_id, anya, borya, _vika, _owner, _fake = env
    await ai_pref.save_pref(anya["id"], borya["id"], mode="draft")
    message_id = await _incoming(thread_id, borya["id"], "ау")
    await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    await _as(client, anya["id"])
    assert (await client.post(f"/api/messages/{thread_id}/ai/dismiss")).status_code == 200
    assert await ai_pref.get_draft(anya["id"], thread_id) is None


@pytest.mark.asyncio
async def test_non_member_cannot_touch_the_ai_panel(env) -> None:
    """Перебор id веток посторонним не даёт ни настройки, ни черновика."""
    client, _db, thread_id, _anya, _borya, vika, _owner, _fake = env
    await _as(client, vika["id"])
    assert (await client.get(f"/api/messages/{thread_id}/ai")).status_code == 404
    assert (
        await client.post(f"/api/messages/{thread_id}/ai", json={"mode": "auto"})
    ).status_code == 404
    assert (
        await client.post(f"/api/messages/{thread_id}/ai/dismiss")
    ).status_code == 404


# ── C. Авто-режим: метка, квота, кулдаун, согласие ──────────────────────────


@pytest.mark.asyncio
async def test_auto_persists_ai_message_labelled_for_both_sides(env) -> None:
    client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    fake.reply = "Спрошу у него и вернусь."
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    message_id = await _incoming(thread_id, borya["id"], "перезвонишь?")

    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)
    assert outcome["action"] == "auto"

    messages = await social.list_messages(thread_id, anya["id"])
    assert [m["kind"] for m in messages] == ["human", "ai"]
    assert messages[-1]["body"] == fake.reply
    assert messages[-1]["sender_id"] == anya["id"]

    # Метка видна ОБЕИМ сторонам — получателя обманывать нельзя.
    for uid in (anya["id"], borya["id"]):
        await _as(client, uid)
        page = await client.get(f"/messages/{thread_id}")
        assert page.status_code == 200
        assert fake.reply in page.text
        assert "✨ ответил ИИ" in page.text
        assert 'data-kind="ai"' in page.text


@pytest.mark.asyncio
async def test_sending_a_message_triggers_the_ai_in_the_background(env) -> None:
    """Сквозной путь: POST /send → фоновая задача → ИИ-ответ в ветке."""
    client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    fake.reply = "Уточню и отвечу."
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)

    await _as(client, borya["id"])
    sent = await client.post(
        f"/api/messages/{thread_id}/send", json={"body": "ты свободен в пятницу?"}
    )
    assert sent.status_code == 200
    # Ответ отправителю НЕ ждёт ни модели, ни SMTP: сообщение уже сохранено.
    assert sent.json()["message"]["kind"] == "human"

    await _drain_background()
    messages = await social.list_messages(thread_id, borya["id"])
    assert [m["kind"] for m in messages] == ["human", "ai"]
    assert messages[-1]["body"] == fake.reply


@pytest.mark.asyncio
async def test_ai_never_replies_to_an_ai_message(env) -> None:
    """Два включённых ассистента не должны разговаривать друг с другом."""
    _client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    # Как будто ИИ Бори уже ответил за него.
    ai_message = await social.send_message(
        thread_id, borya["id"], "это написал ИИ Бори", kind="ai"
    )

    outcome = await ai_reply.handle_incoming(thread_id, int(ai_message["id"]), now=T0)

    assert outcome["action"] == "none"
    assert outcome["reason"] == "peer_is_ai"
    assert fake.calls == [], "на сообщение ИИ модель не должна вызываться вовсе"
    assert await _count_messages(thread_id) == 1


@pytest.mark.asyncio
async def test_daily_cap_degrades_to_draft(env) -> None:
    _client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    await ai_pref.save_pref(
        anya["id"], borya["id"], mode="auto", auto_ack=True, quota_daily=1
    )

    first = await _incoming(thread_id, borya["id"], "раз")
    assert (await ai_reply.handle_incoming(thread_id, first, now=T0))["action"] == "auto"

    # Второе сообщение — заведомо ПОСЛЕ кулдауна, упирается именно в квоту.
    later = T0 + timedelta(seconds=ai_pref.MIN_INTERVAL_SECONDS + 60)
    second = await _incoming(thread_id, borya["id"], "два")
    outcome = await ai_reply.handle_incoming(thread_id, second, now=later)

    assert outcome["action"] == "draft"
    assert outcome["reason"] == "daily_cap"
    # Ровно один ИИ-ответ в ветке (2 входящих + 1 ИИ).
    messages = await social.list_messages(thread_id, anya["id"])
    assert [m["kind"] for m in messages].count("ai") == 1
    assert (await ai_pref.get_draft(anya["id"], thread_id))["body"] == fake.reply


@pytest.mark.asyncio
async def test_cooldown_degrades_to_draft(env) -> None:
    _client, _db, thread_id, anya, borya, _vika, _owner, _fake = env
    await ai_pref.save_pref(
        anya["id"], borya["id"], mode="auto", auto_ack=True, quota_daily=20
    )

    first = await _incoming(thread_id, borya["id"], "раз")
    assert (await ai_reply.handle_incoming(thread_id, first, now=T0))["action"] == "auto"

    too_soon = T0 + timedelta(seconds=ai_pref.MIN_INTERVAL_SECONDS - 1)
    second = await _incoming(thread_id, borya["id"], "два")
    outcome = await ai_reply.handle_incoming(thread_id, second, now=too_soon)

    assert outcome["action"] == "draft"
    assert outcome["reason"] == "cooldown"
    assert [m["kind"] for m in await social.list_messages(thread_id, anya["id"])].count("ai") == 1


@pytest.mark.asyncio
async def test_quota_resets_on_a_new_day(env) -> None:
    _client, _db, thread_id, anya, borya, _vika, _owner, _fake = env
    await ai_pref.save_pref(
        anya["id"], borya["id"], mode="auto", auto_ack=True, quota_daily=1
    )
    first = await _incoming(thread_id, borya["id"], "вчера")
    await ai_reply.handle_incoming(thread_id, first, now=T0)

    tomorrow = T0 + timedelta(days=1)
    second = await _incoming(thread_id, borya["id"], "сегодня")
    outcome = await ai_reply.handle_incoming(thread_id, second, now=tomorrow)

    assert outcome["action"] == "auto"
    pref = await ai_pref.get_pref(anya["id"], borya["id"])
    assert pref["used_today"] == 1  # счётчик начался заново


@pytest.mark.asyncio
async def test_auto_without_explicit_consent_is_stored_as_draft(env) -> None:
    _client, _db, _thread_id, anya, borya, _vika, _owner, _fake = env
    pref = await ai_pref.save_pref(
        anya["id"], borya["id"], mode="auto", auto_ack=False
    )
    assert pref["mode"] == "draft", "auto без галочки не должен сохраняться как auto"

    # …и даже если строка каким-то образом окажется auto без согласия —
    # резолвер всё равно опустит её до черновика.
    forged = dict(pref)
    forged["mode"] = "auto"
    forged["auto_ack"] = False
    decision = ai_pref.resolve_action(forged, T0)  # type: ignore[arg-type]
    assert decision == {"action": "draft", "reason": "not_acknowledged"}


@pytest.mark.asyncio
async def test_consent_checkbox_flows_through_http(env) -> None:
    client, _db, thread_id, anya, borya, _vika, _owner, _fake = env
    await _as(client, anya["id"])

    without = await client.post(
        f"/api/messages/{thread_id}/ai", json={"mode": "auto", "ack": False}
    )
    assert without.json()["pref"]["mode"] == "draft"

    with_ack = await client.post(
        f"/api/messages/{thread_id}/ai",
        json={"mode": "auto", "ack": True, "style_note": "отвечай коротко"},
    )
    body = with_ack.json()["pref"]
    assert body["mode"] == "auto"
    assert body["style_note"] == "отвечай коротко"
    assert (await ai_pref.get_pref(anya["id"], borya["id"]))["mode"] == "auto"


# ── D. Не настроенная модель ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unconfigured_llm_sends_nothing_and_surfaces_a_hint(env) -> None:
    client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    fake.error = LLMNotConfigured("LLM not configured. Pick a provider at /settings/llm")
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    message_id = await _incoming(thread_id, borya["id"], "ответишь?")

    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    assert outcome["action"] == "error"
    assert outcome["reason"] == "llm_not_configured"
    assert await _count_messages(thread_id) == 1  # ничего не отправлено
    assert await ai_pref.get_draft(anya["id"], thread_id) is None

    # Ни 500, ни пустоты: подсказка доезжает до UI.
    await _as(client, anya["id"])
    panel = await client.get(f"/api/messages/{thread_id}/ai")
    assert panel.status_code == 200
    assert "/settings/llm" in panel.json()["pref"]["last_error"]


@pytest.mark.asyncio
async def test_provider_failure_does_not_raise(env) -> None:
    _client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    fake.error = RuntimeError("upstream 503")
    await ai_pref.save_pref(anya["id"], borya["id"], mode="draft")
    message_id = await _incoming(thread_id, borya["id"], "?")

    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)
    assert outcome["action"] == "error"
    assert outcome["reason"] == "generation_failed"
    # dispatch глотает всё — фон не имеет права ронять запрос.
    await ai_reply.dispatch(thread_id, message_id)


# ── E. Граница контекста: канарейки ─────────────────────────────────────────


async def _seed_private_canaries(user_id: int, owner_id: int) -> None:
    """Личные данные ОТВЕЧАЮЩЕГО + характер владельца инстанса."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO chat_session "
            "(user_id, title, created_at, updated_at, summary_up_to_id, "
            " auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (user_id, CANARY_CHAT),
        )
        session_id = cursor.lastrowid
        await conn.execute(
            "INSERT INTO chat_message "
            "(session_id, role, content, created_at, is_streaming, is_pinned, "
            " access_count) VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (session_id, CANARY_CHAT),
        )
        await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (user_id, CANARY_MEMORY),
        )
        await conn.execute(
            "INSERT INTO screenshots "
            "(captured_at, monitor_index, width, height, phash, app_name, "
            " window_title) VALUES (?, 0, 1920, 1080, ?, 'Telegram', ?)",
            ("2026-08-24T10:15:00", CANARY_SHOT, CANARY_SHOT),
        )
        await conn.execute(
            "INSERT INTO audio_segment "
            "(captured_at, ended_at, duration_seconds, codec, path, size_bytes, "
            " transcript) VALUES (?, ?, 60.0, 'opus', ?, 1024, ?)",
            (
                "2026-08-24T10:20:00",
                "2026-08-24T10:21:00",
                "/tmp/canary-dm.opus",
                CANARY_AUDIO,
            ),
        )
        await conn.execute(
            "INSERT INTO notes (body, created_at, updated_at) "
            "VALUES (?, datetime('now'), datetime('now'))",
            (CANARY_NOTE,),
        )
        await conn.execute(
            "INSERT INTO reminders (body, due_date) VALUES (?, ?)",
            (CANARY_REMINDER, "2026-08-25T09:00:00"),
        )
        # Характер ВЛАДЕЛЬЦА инстанса живёт в глобальном kv — участнику он
        # не положен даже как «системный промпт по умолчанию».
        await set_kv(conn, "chat_system_prompt", CANARY_OWNER_PROMPT)
        await conn.commit()


@pytest.mark.asyncio
async def test_prompt_contains_no_private_context(env) -> None:
    client, db, thread_id, anya, borya, _vika, owner_user, fake = env
    await _seed_private_canaries(anya["id"], owner_user["id"])
    # Свой характер участник задал сам — он-то в промпте быть ОБЯЗАН.
    await set_user_kv(db, anya["id"], "chat_system_prompt", "Ты пишешь сухо.")
    templates_engine._user_kv_value_cache.clear()

    await ai_pref.save_pref(
        anya["id"],
        borya["id"],
        mode="auto",
        auto_ack=True,
        style_note="не соглашайся на встречи",
    )
    message_id = await _incoming(thread_id, borya["id"], "давай пересечёмся в пятницу")
    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)
    assert outcome["action"] == "auto"

    prompt = fake.last_prompt
    leaked = [canary for canary in ALL_CANARIES if canary in prompt]
    assert leaked == [], f"в промпт утекли личные данные: {leaked}"

    # …и одновременно в промпте есть ровно то, что там должно быть.
    assert "давай пересечёмся в пятницу" in prompt   # эта ветка
    assert "Боря" in prompt                          # имя собеседника
    assert "не соглашайся на встречи" in prompt      # style_note
    assert "Ты пишешь сухо." in prompt               # СВОЙ характер
    assert "не назначай и не" in prompt              # запрет обязательств
    assert "спрошу у него и вернусь" in prompt       # честное «не знаю»
    assert client is not None


@pytest.mark.asyncio
async def test_prompt_does_not_leak_a_third_partys_thread(env) -> None:
    """Контекст берётся ТОЛЬКО из этой ветки, даже если есть другие."""
    _client, _db, thread_id, anya, borya, vika, _owner, fake = env
    other_request = await social.send_request(anya["id"], vika["id"])
    await social.accept_request(other_request, vika["id"])
    other_thread = await social.get_or_create_thread(anya["id"], vika["id"])
    await social.send_message(other_thread, vika["id"], "ДРУГАЯ-ВЕТКА-СЕКРЕТ")

    await ai_pref.save_pref(anya["id"], borya["id"], mode="draft")
    message_id = await _incoming(thread_id, borya["id"], "как дела?")
    await ai_reply.handle_incoming(thread_id, message_id, now=T0)

    assert "ДРУГАЯ-ВЕТКА-СЕКРЕТ" not in fake.last_prompt


# ── F. Рубильник ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kill_switch_turns_every_conversation_off(env) -> None:
    client, _db, thread_id, anya, borya, vika, _owner, _fake = env
    other_request = await social.send_request(anya["id"], vika["id"])
    await social.accept_request(other_request, vika["id"])

    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    await ai_pref.save_pref(anya["id"], vika["id"], mode="draft")
    # Настройка ДРУГОГО человека рубильником Ани не трогается.
    await ai_pref.save_pref(borya["id"], anya["id"], mode="draft")

    await _as(client, anya["id"])
    response = await client.post("/api/messages/ai/off-everywhere")
    assert response.status_code == 200
    assert response.json()["changed"] == 2

    assert await ai_pref.list_active(anya["id"]) == []
    mine = await ai_pref.get_pref(anya["id"], borya["id"])
    assert mine["mode"] == "off"
    # Согласие тоже снято: обратное включение потребует новой галочки.
    assert mine["auto_ack"] is False
    assert (await ai_pref.get_pref(borya["id"], anya["id"]))["mode"] == "draft"

    # После рубильника входящее больше ничего не запускает.
    message_id = await _incoming(thread_id, borya["id"], "ещё раз")
    assert (await ai_reply.handle_incoming(thread_id, message_id, now=T0))["action"] == "none"


@pytest.mark.asyncio
async def test_turning_the_mode_off_does_not_reset_the_daily_counter(env) -> None:
    """«Выключил — включил» не должно быть обходом дневной квоты."""
    _client, _db, thread_id, anya, borya, _vika, _owner, _fake = env
    await ai_pref.save_pref(
        anya["id"], borya["id"], mode="auto", auto_ack=True, quota_daily=1
    )
    first = await _incoming(thread_id, borya["id"], "раз")
    await ai_reply.handle_incoming(thread_id, first, now=T0)

    await ai_pref.save_pref(anya["id"], borya["id"], mode="off")
    await ai_pref.save_pref(
        anya["id"], borya["id"], mode="auto", auto_ack=True, quota_daily=1
    )

    later = T0 + timedelta(seconds=ai_pref.MIN_INTERVAL_SECONDS + 60)
    second = await _incoming(thread_id, borya["id"], "два")
    outcome = await ai_reply.handle_incoming(thread_id, second, now=later)
    assert outcome["reason"] == "daily_cap"


@pytest.mark.asyncio
async def test_unfriending_stops_the_ai(env) -> None:
    """Разорвали дружбу — резолвер доступа закрывает и ИИ-ход тоже."""
    _client, _db, thread_id, anya, borya, _vika, _owner, fake = env
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    message_id = await _incoming(thread_id, borya["id"], "пока ещё друзья")
    await social.unfriend(anya["id"], borya["id"])

    outcome = await ai_reply.handle_incoming(thread_id, message_id, now=T0)
    assert outcome["action"] == "none"
    assert outcome["reason"] == "thread_closed"
    assert fake.calls == []
