"""Уведомления социального слоя: изоляция, дефолты, антиспам, рубильник.

Что здесь закрепляется (менять только вместе с
``app/social/notifications.py`` и миграцией 231):

* дефолт — ТОЛЬКО браузер; почта и Telegram выключены, пока их не включили
  руками (они выходят за пределы инстанса);
* настройки персональные: включённая почта одного человека НИКОГДА не
  включается другому, а очередь браузерных уведомлений не пересекается;
* письмо про одну переписку — не чаще раза в окно, даже если сообщений
  прилетело двадцать;
* Telegram-токен пользователя A физически не может уйти на отправку
  пользователю B, а наружу полный токен не отдаётся вообще;
* рубильник «выключить ИИ везде» гасит все переписки владельца настройки
  и не трогает чужие.

Сеть не дёргается ни разу: SMTP и Telegram-транспорт замоканы. Время
передаётся явно — ни одного ``sleep``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.social import ai_pref, notifications
from app.social import repository as social
from app.storage.repository import set_kv
from app.web import rate_limit, templates_engine
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware

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

    from app.web.routes import dm_ai, friends, messages, social_notifications

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(friends.router)
    app.include_router(messages.router)
    app.include_router(dm_ai.router)
    app.include_router(social_notifications.router)
    return app


class MailSpy:
    """Подставной SMTP: письма никуда не уходят, только записываются."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_email(
        self, to_addr: str, subject: str, body_text: str, body_html: str | None = None
    ) -> dict[str, Any]:
        self.sent.append((to_addr, subject, body_text))
        return {"status": "sent", "to": to_addr}


class TelegramSpy:
    """Подставной транспорт Telegram: запоминает, ЧЬИМ токеном что ушло."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, int, str]] = []

    def factory(spy, token: str):
        class _API:
            def __init__(self) -> None:
                self.token = token

            async def send_message(self, chat_id: int, text: str, **_kw):
                spy.sent.append((token, int(chat_id), text))
                return (1,)

        return _API()


@pytest_asyncio.fixture
async def env(db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch):
    """Владелец + Аня и Боря (друзья) + подставные SMTP и Telegram."""
    owner_user = await create_user("owner@notif.test", "Zq7-frost-lantern-91", "Владелец")
    anya = await create_user("anya@notif.test", "Kp4-velvet-harbour-38", "Аня")
    borya = await create_user("borya@notif.test", "Kp4-velvet-harbour-38", "Боря")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    _reset_caches()

    request_id = await social.send_request(anya["id"], borya["id"])
    assert await social.accept_request(request_id, borya["id"])

    mail = MailSpy()
    telegram = TelegramSpy()
    import app.integrations.telegram.api as tg_api
    import app.smtp_delivery as smtp

    monkeypatch.setattr(smtp, "send_email", mail.send_email)
    monkeypatch.setattr(tg_api, "TelegramBotAPI", telegram.factory)

    transport = ASGITransport(app=_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, anya, borya, owner_user, mail, telegram
    finally:
        _reset_caches()


async def _drain_background() -> None:
    """Дождаться fire-and-forget задач, которые роуты пускают через create_task.

    Уведомления и ход ИИ намеренно НЕ блокируют HTTP-ответ (см. ``dispatch``),
    поэтому после запроса их надо дождаться явно. Ждём только СВОИ задачи —
    по имени, чтобы не залипнуть на чужих фоновых петлях.
    """
    for _ in range(50):
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and (task.get_name() or "").startswith(("social-notify-", "dm-ai-reply-"))
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=5)


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


def _all_on(*channels: str) -> notifications.Prefs:
    return {
        event: {channel: channel in channels for channel in notifications.CHANNELS}
        for event in notifications.EVENTS
    }


# ── A. Дефолты ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_defaults_are_browser_only(env) -> None:
    _client, _db, anya, _borya, _owner, mail, telegram = env
    prefs = await notifications.get_prefs(anya["id"])

    for event in notifications.EVENTS:
        assert prefs[event]["browser"] is True, event
        assert prefs[event]["email"] is False, event
        assert prefs[event]["telegram"] is False, event

    result = await notifications.notify(
        anya["id"], "dm_message", title="привет", now=T0
    )
    assert result == {"browser": "queued"}
    assert mail.sent == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_browser_queue_is_drained_once(env) -> None:
    _client, _db, anya, _borya, _owner, _mail, _tg = env
    await notifications.notify(anya["id"], "dm_message", title="раз", now=T0)
    await notifications.notify(anya["id"], "dm_message", title="два", now=T0)

    first = await notifications.take_pending(anya["id"])
    assert [item["title"] for item in first] == ["раз", "два"]
    # Показали — второй раз не всплывает (иначе каждая вкладка дублировала бы).
    assert await notifications.take_pending(anya["id"]) == []


# ── B. Изоляция между людьми ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prefs_never_cross_between_users(env) -> None:
    _client, _db, anya, borya, _owner, mail, _tg = env
    await notifications.set_prefs(anya["id"], _all_on("browser", "email"), now=T0)

    assert (await notifications.get_prefs(borya["id"]))["dm_message"]["email"] is False

    await notifications.notify(borya["id"], "dm_message", title="Боре", now=T0)
    assert mail.sent == [], "включённая почта Ани не должна слать письма Боре"

    await notifications.notify(anya["id"], "dm_message", title="Ане", now=T0)
    assert [to for to, _s, _b in mail.sent] == ["anya@notif.test"]


@pytest.mark.asyncio
async def test_browser_queue_never_crosses(env) -> None:
    _client, _db, anya, borya, _owner, _mail, _tg = env
    await notifications.notify(anya["id"], "friend_request", title="только Ане", now=T0)

    assert await notifications.take_pending(borya["id"]) == []
    assert [i["title"] for i in await notifications.take_pending(anya["id"])] == [
        "только Ане"
    ]


@pytest.mark.asyncio
async def test_pending_endpoint_is_scoped_to_the_session(env) -> None:
    client, _db, anya, borya, _owner, _mail, _tg = env
    await notifications.notify(anya["id"], "dm_message", title="АНИНО-СОБЫТИЕ", now=T0)

    await _as(client, borya["id"])
    theirs = await client.get("/api/social-notif/pending")
    assert theirs.status_code == 200
    assert theirs.json()["notifications"] == []
    assert "АНИНО-СОБЫТИЕ" not in theirs.text

    await _as(client, anya["id"])
    mine = await client.get("/api/social-notif/pending")
    assert [i["title"] for i in mine.json()["notifications"]] == ["АНИНО-СОБЫТИЕ"]


@pytest.mark.asyncio
async def test_per_event_channels_are_independent(env) -> None:
    _client, _db, anya, _borya, _owner, mail, _tg = env
    prefs = notifications.default_prefs()
    prefs["friend_request"]["email"] = True   # письма только про заявки
    prefs["dm_message"]["email"] = False
    await notifications.set_prefs(anya["id"], prefs, now=T0)

    await notifications.notify(anya["id"], "dm_message", title="сообщение", now=T0)
    assert mail.sent == []
    await notifications.notify(anya["id"], "friend_request", title="заявка", now=T0)
    assert [s for _t, s, _b in mail.sent] == ["заявка"]


# ── C. Антиспам почты ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_email_cooldown_holds_per_conversation(env) -> None:
    _client, _db, anya, _borya, _owner, mail, _tg = env
    await notifications.set_prefs(anya["id"], _all_on("browser", "email"), now=T0)

    for index in range(5):
        await notifications.notify(
            anya["id"],
            "dm_message",
            title=f"сообщение {index}",
            scope="dm:7",
            now=T0 + timedelta(seconds=index * 10),
        )
    assert len(mail.sent) == 1, "пять сообщений подряд = одно письмо"

    # Другая переписка — своё окно, письмо уходит сразу.
    await notifications.notify(
        anya["id"], "dm_message", title="из другой ветки", scope="dm:8", now=T0
    )
    assert len(mail.sent) == 2

    # Окно истекло — снова можно.
    later = T0 + timedelta(seconds=notifications.EMAIL_COOLDOWN_SECONDS + 1)
    await notifications.notify(
        anya["id"], "dm_message", title="через 10 минут", scope="dm:7", now=later
    )
    assert [s for _t, s, _b in mail.sent] == [
        "сообщение 0",
        "из другой ветки",
        "через 10 минут",
    ]


@pytest.mark.asyncio
async def test_email_cooldown_is_per_user(env) -> None:
    _client, _db, anya, borya, _owner, mail, _tg = env
    both = _all_on("browser", "email")
    await notifications.set_prefs(anya["id"], both, now=T0)
    await notifications.set_prefs(borya["id"], both, now=T0)

    await notifications.notify(anya["id"], "dm_message", title="A", scope="dm:7", now=T0)
    # Тот же scope, но ДРУГОЙ человек — его окно ещё не начиналось.
    await notifications.notify(borya["id"], "dm_message", title="B", scope="dm:7", now=T0)

    assert sorted(to for to, _s, _b in mail.sent) == [
        "anya@notif.test",
        "borya@notif.test",
    ]


# ── D. Telegram: свой бот у каждого ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_telegram_token_of_one_user_is_never_used_for_another(env) -> None:
    _client, _db, anya, borya, _owner, _mail, telegram = env
    await notifications.set_telegram_config(anya["id"], "AAA:anya-token", "111")
    await notifications.set_prefs(anya["id"], _all_on("telegram"), now=T0)
    await notifications.set_prefs(borya["id"], _all_on("telegram"), now=T0)

    # У Бори бот НЕ настроен — его уведомление не должно уйти чужим токеном.
    result = await notifications.notify(borya["id"], "dm_message", title="Боре", now=T0)
    assert result["telegram"] == "not_configured"
    assert telegram.sent == []

    await notifications.notify(anya["id"], "dm_message", title="Ане", now=T0)
    assert [token for token, _chat, _text in telegram.sent] == ["AAA:anya-token"]
    assert [chat for _token, chat, _text in telegram.sent] == [111]

    # У Бори свой токен — и уходит именно он.
    await notifications.set_telegram_config(borya["id"], "BBB:borya-token", "222")
    await notifications.notify(borya["id"], "dm_message", title="Боре снова", now=T0)
    assert telegram.sent[-1][0] == "BBB:borya-token"
    assert telegram.sent[-1][1] == 222


@pytest.mark.asyncio
async def test_full_token_is_never_returned_or_rendered(env) -> None:
    client, _db, anya, _borya, _owner, _mail, _tg = env
    secret = "9999:SUPER-SECRET-BOT-TOKEN"
    await notifications.set_telegram_config(anya["id"], secret, "111")

    config = await notifications.get_telegram_config(anya["id"])
    assert config["configured"] is True
    assert config["chat_id"] == "111"
    assert config["token_tail"] == secret[-4:]
    assert secret not in str(config)

    await _as(client, anya["id"])
    page = await client.get("/settings/notifications-social")
    assert page.status_code == 200
    assert secret not in page.text


@pytest.mark.asyncio
async def test_telegram_test_button_uses_only_my_own_config(env) -> None:
    client, _db, anya, borya, _owner, _mail, telegram = env
    await notifications.set_telegram_config(anya["id"], "AAA:anya-token", "111")

    await _as(client, borya["id"])
    theirs = await client.post("/api/social-notif/telegram/test")
    assert theirs.json()["status"] == "not_configured"
    assert telegram.sent == []

    await _as(client, anya["id"])
    mine = await client.post("/api/social-notif/telegram/test")
    assert mine.json()["status"] == "sent"
    assert telegram.sent[-1][0] == "AAA:anya-token"


@pytest.mark.asyncio
async def test_telegram_failure_never_raises(env, monkeypatch) -> None:
    _client, _db, anya, _borya, _owner, _mail, _tg = env
    await notifications.set_telegram_config(anya["id"], "AAA:token", "111")
    await notifications.set_prefs(anya["id"], _all_on("telegram"), now=T0)

    import app.integrations.telegram.api as tg_api

    def _explode(_token: str):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(tg_api, "TelegramBotAPI", _explode)
    result = await notifications.notify(anya["id"], "dm_message", title="х", now=T0)
    assert result["telegram"] == "failed"


# ── E. Страница настроек ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_page_is_member_surface(env) -> None:
    for path in ("/settings/notifications-social", "/api/social-notif/pending"):
        assert auth_gate._is_member_path(path) is True, path
    assert auth_gate._is_member_path("/settings/notifications-socialXXX") is False


@pytest.mark.asyncio
async def test_settings_page_saves_the_matrix(env) -> None:
    client, _db, anya, _borya, _owner, _mail, _tg = env
    await _as(client, anya["id"])

    response = await client.post(
        "/settings/notifications-social",
        data={
            "friend_request__browser": "on",
            "friend_request__email": "on",
            "dm_message__telegram": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    prefs = await notifications.get_prefs(anya["id"])
    assert prefs["friend_request"]["email"] is True
    assert prefs["friend_request"]["telegram"] is False
    assert prefs["dm_message"]["telegram"] is True
    # Снятая галочка сохраняется ЯВНО как «выключено», а не «не трогали».
    assert prefs["dm_message"]["browser"] is False


@pytest.mark.asyncio
async def test_saving_the_matrix_does_not_erase_the_telegram_token(env) -> None:
    """Пустое поле токена не должно отвязывать бота при каждом сохранении."""
    client, _db, anya, _borya, _owner, _mail, _tg = env
    await notifications.set_telegram_config(anya["id"], "AAA:keep-me", "111")

    await _as(client, anya["id"])
    await client.post(
        "/settings/notifications-social",
        data={"dm_message__browser": "on", "tg_token": "", "tg_chat_id": "111"},
        follow_redirects=False,
    )
    assert (await notifications.get_telegram_config(anya["id"]))["configured"] is True

    # …а явная отвязка — работает.
    await client.post(
        "/settings/notifications-social",
        data={"dm_message__browser": "on", "tg_clear": "on"},
        follow_redirects=False,
    )
    assert (await notifications.get_telegram_config(anya["id"]))["configured"] is False


# ── F. События социального слоя ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_friend_request_and_accept_reach_the_right_person(env) -> None:
    client, _db, anya, borya, _owner, _mail, _tg = env
    vika = await create_user("vika@notif.test", "Kp4-velvet-harbour-38", "Вика")

    await _as(client, anya["id"])
    sent = await client.post("/api/friends/request", json={"to_user_id": vika["id"]})
    assert sent.status_code == 200
    request_id = sent.json()["request_id"]
    await _drain_background()

    # Уведомление ушло ВИКЕ (адресату), а не отправителю.
    assert await notifications.take_pending(anya["id"]) == []
    incoming = await notifications.take_pending(vika["id"])
    assert [i["event"] for i in incoming] == ["friend_request"]
    assert "Аня" in incoming[0]["title"]

    await _as(client, vika["id"])
    assert (await client.post(f"/api/friends/{request_id}/accept")).status_code == 200
    await _drain_background()

    accepted = await notifications.take_pending(anya["id"])
    assert [i["event"] for i in accepted] == ["friend_accepted"]
    assert "Вика" in accepted[0]["title"]
    assert borya is not None


@pytest.mark.asyncio
async def test_new_dm_notifies_the_recipient_only(env) -> None:
    _client, _db, anya, borya, _owner, _mail, _tg = env
    from app.social import ai_reply

    thread_id = await social.get_or_create_thread(anya["id"], borya["id"])
    message = await social.send_message(thread_id, anya["id"], "йо")
    await ai_reply.notify_incoming(thread_id, int(message["id"]))

    assert await notifications.take_pending(anya["id"]) == []
    theirs = await notifications.take_pending(borya["id"])
    assert [i["event"] for i in theirs] == ["dm_message"]
    assert "Аня" in theirs[0]["title"]
    assert theirs[0]["url"] == f"/messages/{thread_id}"


@pytest.mark.asyncio
async def test_ai_reply_notifies_its_owner_and_the_peer(env, monkeypatch) -> None:
    """«Твой ИИ ответил за тебя» — отдельное событие для владельца настройки."""
    _client, _db, anya, borya, _owner, _mail, _tg = env
    from app.social import ai_reply

    class _Client:
        provider = "fake"

        async def complete(self, _request):
            return "Спрошу у него и вернусь."

    monkeypatch.setattr(ai_reply, "make_client", lambda **_kw: _Client())

    thread_id = await social.get_or_create_thread(anya["id"], borya["id"])
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    message = await social.send_message(thread_id, borya["id"], "перезвонишь?")
    outcome = await ai_reply.handle_incoming(thread_id, int(message["id"]), now=T0)
    assert outcome["action"] == "auto"

    mine = await notifications.take_pending(anya["id"])
    assert [i["event"] for i in mine] == ["ai_replied"]
    assert "Боря" in mine[0]["title"]

    theirs = await notifications.take_pending(borya["id"])
    assert [i["event"] for i in theirs] == ["dm_message"]
    assert "✨" in theirs[0]["title"], "получателю видно, что писал ИИ"


# ── G. Рубильник ИИ со страницы уведомлений ─────────────────────────────────


@pytest.mark.asyncio
async def test_kill_switch_from_the_settings_page(env) -> None:
    client, _db, anya, borya, _owner, _mail, _tg = env
    await ai_pref.save_pref(anya["id"], borya["id"], mode="auto", auto_ack=True)
    await ai_pref.save_pref(borya["id"], anya["id"], mode="auto", auto_ack=True)

    await _as(client, anya["id"])
    page = await client.get("/settings/notifications-social")
    assert page.status_code == 200
    assert "Боря" in page.text  # видно, ЧТО именно будет выключено

    response = await client.post("/api/messages/ai/off-everywhere")
    assert response.status_code == 200

    assert await ai_pref.list_active(anya["id"]) == []
    # Настройка Бори — его собственная, рубильник Ани её не трогает.
    assert [p["mode"] for p in await ai_pref.list_active(borya["id"])] == ["auto"]
