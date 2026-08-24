"""Социальный слой: друзья + личные сообщения. Приватность и IDOR.

Что здесь закрепляется (менять только вместе с
``app/social/repository.py`` и миграцией 229):

* поиск НЕ отдаёт e-mail, НЕ отдаёт невидимых (``social_discoverable=0``)
  и по умолчанию НЕ отдаёт владельца инстанса;
* заявка → принятие даёт дружбу В ОБЕ СТОРОНЫ и позволяет открыть ветку;
* отказ/отмена работают, и после отказа можно попроситься снова;
* НЕ-друг не может ни открыть, ни прочитать, ни написать в ветку — в том
  числе перебором id (IDOR): ответ 404, а не 403, чтобы существование
  чужой переписки не подтверждалось;
* сообщения листаются постранично, счётчик непрочитанного персональный;
* ``kind='ai'`` рисуется с меткой «✨ ответил ИИ»;
* rate-limit поиска срабатывает на переборе.

kv ``owner_exclusive_mode`` ВЫКЛ — иначе гейт паркует всех не-владельцев на
/pending (отдельный kill-switch, см. test_owner_exclusive_lockdown.py).
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.social import repository as social
from app.storage.repository import set_kv, set_user_kv
from app.web import rate_limit, templates_engine
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware


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
    # Скользящее окно rate-limit процесс-глобальное: без сброса «перебор» из
    # одного теста отравил бы соседний.
    rate_limit._EVENTS.clear()


def _app():
    from fastapi import FastAPI

    from app.web.routes import friends, messages

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(friends.router)
    app.include_router(messages.router)
    return app


@pytest_asyncio.fixture
async def env(db: aiosqlite.Connection):
    """Владелец + три участника: Аня, Боря, Вика."""
    owner_user = await create_user("owner@social.test", "owner-pass-123", "Владелец")
    anya = await create_user("anya@social.test", "member-pass-123", "Аня")
    borya = await create_user("borya@social.test", "member-pass-123", "Боря")
    vika = await create_user("vika@social.test", "member-pass-123")  # без имени
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    _reset_caches()

    transport = ASGITransport(app=_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, owner_user, anya, borya, vika
    finally:
        _reset_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


async def _befriend(a: int, b: int) -> int:
    """Сквозной путь заявка → принятие. Возвращает id ветки переписки."""
    request_id = await social.send_request(a, b)
    assert await social.accept_request(request_id, b)
    return await social.get_or_create_thread(a, b)


# ── A. Поиск людей: приватность ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_never_returns_emails(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    await _as(client, anya["id"])

    r = await client.get("/api/friends/search", params={"q": "borya@social.test"})
    assert r.status_code == 200
    body = r.json()
    assert [x["id"] for x in body["results"]] == [borya["id"]]
    assert "borya@social.test" not in r.text
    assert all("email" not in x for x in body["results"])


@pytest.mark.asyncio
async def test_search_masks_email_when_no_display_name(env) -> None:
    client, _db, _owner_user, anya, _borya, vika = env
    await _as(client, anya["id"])

    r = await client.get("/api/friends/search", params={"q": "vika@social.test"})
    results = r.json()["results"]
    assert [x["name"] for x in results] == ["v***@social.test"]


@pytest.mark.asyncio
async def test_search_matches_name_case_insensitively_but_not_two_chars(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    await _as(client, anya["id"])

    hit = await client.get("/api/friends/search", params={"q": "бор"})
    assert [x["id"] for x in hit.json()["results"]] == [borya["id"]]

    # Меньше NAME_MIN_CHARS — молчим, а не отдаём всю базу.
    short = await client.get("/api/friends/search", params={"q": "бо"})
    assert short.json()["results"] == []


@pytest.mark.asyncio
async def test_search_partial_email_does_not_enumerate(env) -> None:
    """Домен/огрызок адреса НЕ матчится: базу по ``@social.test`` не собрать."""
    client, _db, _owner_user, anya, _borya, _vika = env
    await _as(client, anya["id"])

    for probe in ("@social.test", "social.test", "bor@social.test"):
        r = await client.get("/api/friends/search", params={"q": probe})
        assert r.json()["results"] == [], probe


@pytest.mark.asyncio
async def test_owner_is_not_discoverable_by_default_but_can_opt_in(env) -> None:
    client, db, owner_user, anya, _borya, _vika = env
    await _as(client, anya["id"])

    hidden = await client.get("/api/friends/search", params={"q": "owner@social.test"})
    assert hidden.json()["results"] == []

    await set_user_kv(db, owner_user["id"], social.DISCOVERABLE_KEY, "1")
    shown = await client.get("/api/friends/search", params={"q": "owner@social.test"})
    assert [x["id"] for x in shown.json()["results"]] == [owner_user["id"]]


@pytest.mark.asyncio
async def test_member_who_opted_out_is_unfindable_even_by_exact_email(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    await social.set_discoverable(borya["id"], False)
    await _as(client, anya["id"])

    by_email = await client.get("/api/friends/search", params={"q": "borya@social.test"})
    assert by_email.json()["results"] == []
    by_name = await client.get("/api/friends/search", params={"q": "боря"})
    assert by_name.json()["results"] == []

    # …и прямой POST по id тоже не проходит: «ненаходим» значит недоступен.
    direct = await client.post(
        "/api/friends/request", json={"to_user_id": borya["id"]}
    )
    assert direct.status_code == 400


@pytest.mark.asyncio
async def test_search_hides_someone_who_declined_me(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    request_id = await social.send_request(anya["id"], borya["id"])
    assert await social.decline_request(request_id, borya["id"])

    await _as(client, anya["id"])
    mine = await client.get("/api/friends/search", params={"q": "borya@social.test"})
    assert mine.json()["results"] == []

    # …но для ТРЕТЬЕГО человека Боря по-прежнему находится: отказ приватен.
    await _as(client, (await create_user("kto@social.test", "member-pass-123"))["id"])
    other = await client.get("/api/friends/search", params={"q": "borya@social.test"})
    assert [x["id"] for x in other.json()["results"]] == [borya["id"]]


@pytest.mark.asyncio
async def test_search_excludes_self(env) -> None:
    client, _db, _owner_user, anya, _borya, _vika = env
    await _as(client, anya["id"])
    r = await client.get("/api/friends/search", params={"q": "anya@social.test"})
    assert r.json()["results"] == []


@pytest.mark.asyncio
async def test_search_rate_limit_fires_on_abuse(env) -> None:
    client, _db, _owner_user, anya, _borya, _vika = env
    await _as(client, anya["id"])

    statuses = []
    for i in range(40):
        r = await client.get("/api/friends/search", params={"q": f"probe{i}@x.test"})
        statuses.append(r.status_code)
    assert 429 in statuses, "перебор адресов должен упереться в лимит"
    assert statuses[0] == 200


# ── B. Заявки и дружба ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_accept_creates_friendship_both_ways(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    await _as(client, anya["id"])
    sent = await client.post(
        "/api/friends/request", json={"to_user_id": borya["id"], "message": "привет"}
    )
    assert sent.status_code == 200
    request_id = sent.json()["request_id"]

    incoming = await social.list_incoming(borya["id"])
    assert [x["id"] for x in incoming] == [request_id]
    assert incoming[0]["message"] == "привет"

    await _as(client, borya["id"])
    assert (await client.post(f"/api/friends/{request_id}/accept")).status_code == 200

    assert await social.are_friends(anya["id"], borya["id"])
    assert await social.are_friends(borya["id"], anya["id"])
    assert [f["id"] for f in await social.list_friends(anya["id"])] == [borya["id"]]
    assert [f["id"] for f in await social.list_friends(borya["id"])] == [anya["id"]]


@pytest.mark.asyncio
async def test_only_recipient_can_accept(env) -> None:
    client, _db, _owner_user, anya, borya, vika = env
    request_id = await social.send_request(anya["id"], borya["id"])

    # Отправитель не может принять сам себе.
    await _as(client, anya["id"])
    assert (await client.post(f"/api/friends/{request_id}/accept")).status_code == 404
    # Посторонний — тем более.
    await _as(client, vika["id"])
    assert (await client.post(f"/api/friends/{request_id}/accept")).status_code == 404
    assert not await social.are_friends(anya["id"], borya["id"])


@pytest.mark.asyncio
async def test_decline_then_request_again_is_possible(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    first = await social.send_request(anya["id"], borya["id"])

    await _as(client, borya["id"])
    assert (await client.post(f"/api/friends/{first}/decline")).status_code == 200
    assert await social.list_incoming(borya["id"]) == []

    # Повторная заявка не создаёт вторую строку — оживляет ту же.
    second = await social.send_request(anya["id"], borya["id"], "ну пожалуйста")
    assert second == first
    assert len(await social.list_incoming(borya["id"])) == 1
    assert await social.accept_request(second, borya["id"])
    assert await social.are_friends(anya["id"], borya["id"])


@pytest.mark.asyncio
async def test_cancel_removes_outgoing_request(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    request_id = await social.send_request(anya["id"], borya["id"])

    # Получатель не может «отменить» чужую исходящую (только отклонить).
    await _as(client, borya["id"])
    assert (await client.post(f"/api/friends/{request_id}/cancel")).status_code == 404

    await _as(client, anya["id"])
    assert (await client.post(f"/api/friends/{request_id}/cancel")).status_code == 200
    assert await social.list_outgoing(anya["id"]) == []
    assert await social.list_incoming(borya["id"]) == []


@pytest.mark.asyncio
async def test_mutual_requests_become_friendship(env) -> None:
    """Встречная заявка = взаимность: дружим сразу, второй заявки не плодим."""
    _client, _db, _owner_user, anya, borya, _vika = env
    await social.send_request(anya["id"], borya["id"])
    await social.send_request(borya["id"], anya["id"])
    assert await social.are_friends(anya["id"], borya["id"])


@pytest.mark.asyncio
async def test_unfriend_removes_both_rows_and_locks_the_thread(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    thread_id = await _befriend(anya["id"], borya["id"])
    await social.send_message(thread_id, anya["id"], "до ссоры")

    await _as(client, anya["id"])
    assert (await client.post(f"/api/friends/{borya['id']}/remove")).status_code == 200

    assert not await social.are_friends(anya["id"], borya["id"])
    assert not await social.are_friends(borya["id"], anya["id"])
    # Ветка закрыта для ОБОИХ, а не только для инициатора разрыва.
    for uid in (anya["id"], borya["id"]):
        with pytest.raises(social.ThreadAccessError):
            await social.list_messages(thread_id, uid)
    assert await social.list_threads(borya["id"]) == []


# ── C. Переписка: доступ и IDOR ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_friend_cannot_open_or_post_to_thread(env) -> None:
    client, _db, _owner_user, anya, borya, vika = env
    thread_id = await _befriend(anya["id"], borya["id"])
    await social.send_message(thread_id, anya["id"], "секрет для двоих")

    await _as(client, vika["id"])
    # HTML: чужую ветку не показываем, уводим в список (не 200 с чужим текстом).
    page = await client.get(f"/messages/{thread_id}", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/messages"
    # JSON: 404 (не 403) — существование чужой ветки не подтверждаем.
    assert (await client.get(f"/api/messages/{thread_id}/poll")).status_code == 404
    assert (await client.get(f"/api/messages/{thread_id}/older")).status_code == 404
    send = await client.post(f"/api/messages/{thread_id}/send", json={"body": "влезаю"})
    assert send.status_code == 404
    assert "секрет для двоих" not in page.text


@pytest.mark.asyncio
async def test_idor_thread_id_guessing_finds_nothing(env) -> None:
    """Перебор id веток посторонним не даёт ни одного 200."""
    client, _db, _owner_user, anya, borya, vika = env
    await _befriend(anya["id"], borya["id"])

    await _as(client, vika["id"])
    for guess in range(1, 12):
        r = await client.get(f"/api/messages/{guess}/poll")
        assert r.status_code == 404, guess


@pytest.mark.asyncio
async def test_member_cannot_read_another_pairs_thread_even_as_friend_of_one(env) -> None:
    client, _db, _owner_user, anya, borya, vika = env
    secret_thread = await _befriend(anya["id"], borya["id"])
    await social.send_message(secret_thread, borya["id"], "только между нами")
    # Вика дружит с Аней, но это НЕ даёт доступа к ветке Ани с Борей.
    await _befriend(anya["id"], vika["id"])

    await _as(client, vika["id"])
    assert (await client.get(f"/api/messages/{secret_thread}/poll")).status_code == 404
    listing = await client.get("/messages")
    assert "только между нами" not in listing.text


@pytest.mark.asyncio
async def test_thread_cannot_be_opened_with_non_friend(env) -> None:
    client, _db, _owner_user, anya, _borya, vika = env
    with pytest.raises(social.ThreadAccessError):
        await social.get_or_create_thread(anya["id"], vika["id"])

    await _as(client, anya["id"])
    r = await client.get(f"/messages/with/{vika['id']}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/friends"


@pytest.mark.asyncio
async def test_thread_is_canonical_regardless_of_who_opens_it(env) -> None:
    _client, _db, _owner_user, anya, borya, _vika = env
    first = await _befriend(anya["id"], borya["id"])
    assert await social.get_or_create_thread(borya["id"], anya["id"]) == first


# ── D. Сообщения: пагинация, непрочитанное, kind='ai' ───────────────────────


@pytest.mark.asyncio
async def test_messages_paginate(env) -> None:
    _client, _db, _owner_user, anya, borya, _vika = env
    thread_id = await _befriend(anya["id"], borya["id"])
    for i in range(25):
        await social.send_message(thread_id, anya["id"], f"сообщение {i}")

    tail = await social.list_messages(thread_id, borya["id"], limit=10)
    assert [m["body"] for m in tail] == [f"сообщение {i}" for i in range(15, 25)]

    older = await social.list_messages(
        thread_id, borya["id"], before_id=tail[0]["id"], limit=10
    )
    assert [m["body"] for m in older] == [f"сообщение {i}" for i in range(5, 15)]

    fresh = await social.list_messages(
        thread_id, borya["id"], after_id=tail[-1]["id"]
    )
    assert fresh == []


@pytest.mark.asyncio
async def test_unread_count_is_per_user(env) -> None:
    client, _db, _owner_user, anya, borya, vika = env
    thread_id = await _befriend(anya["id"], borya["id"])
    await social.send_message(thread_id, anya["id"], "раз")
    await social.send_message(thread_id, anya["id"], "два")

    await _as(client, borya["id"])
    assert (await client.get("/api/messages/unread.json")).json()["unread"] == 2
    # Автор своих сообщений непрочитанного не копит.
    await _as(client, anya["id"])
    assert (await client.get("/api/messages/unread.json")).json()["unread"] == 0
    # Посторонний не видит чужого счётчика вовсе.
    await _as(client, vika["id"])
    assert (await client.get("/api/messages/unread.json")).json()["unread"] == 0

    # Открыл ветку — прочитал.
    await _as(client, borya["id"])
    assert (await client.get(f"/messages/{thread_id}")).status_code == 200
    assert (await client.get("/api/messages/unread.json")).json()["unread"] == 0


@pytest.mark.asyncio
async def test_send_and_poll_roundtrip(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    thread_id = await _befriend(anya["id"], borya["id"])

    await _as(client, anya["id"])
    sent = await client.post(f"/api/messages/{thread_id}/send", json={"body": "  йо  "})
    assert sent.status_code == 200
    message = sent.json()["message"]
    assert message["body"] == "йо"  # обрезали пробелы
    assert message["kind"] == "human"

    empty = await client.post(f"/api/messages/{thread_id}/send", json={"body": "   "})
    assert empty.status_code == 400

    await _as(client, borya["id"])
    polled = await client.get(f"/api/messages/{thread_id}/poll", params={"after_id": 0})
    bodies = [m["body"] for m in polled.json()["messages"]]
    assert bodies == ["йо"]
    assert polled.json()["messages"][0]["mine"] is False


@pytest.mark.asyncio
async def test_ai_message_renders_with_label(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    thread_id = await _befriend(anya["id"], borya["id"])
    ai = await social.send_message(
        thread_id, anya["id"], "это написал ИИ за меня", kind="ai"
    )
    assert ai["kind"] == "ai"

    await _as(client, borya["id"])
    page = await client.get(f"/messages/{thread_id}")
    assert page.status_code == 200
    assert "это написал ИИ за меня" in page.text
    # Метка ИИ и её визуальная ветка присутствуют в отрисованном пузыре.
    assert "✨ ответил ИИ" in page.text
    assert 'data-kind="ai"' in page.text
    assert "violet" in page.text

    # …а у обычного сообщения ИИ-пузыря нет. Сравниваем по ``data-kind``:
    # текст метки лежит и в Alpine-шаблоне (для сообщений, прилетевших после
    # загрузки), поэтому «нет строки на странице» тут ничего не доказывало бы.
    plain_thread = await _befriend(borya["id"], _vika_id(env))
    await social.send_message(plain_thread, borya["id"], "обычный текст")
    plain = await client.get(f"/messages/{plain_thread}")
    assert "обычный текст" in plain.text
    assert 'data-kind="human"' in plain.text
    assert 'data-kind="ai"' not in plain.text


def _vika_id(env) -> int:
    return env[5]["id"]


@pytest.mark.asyncio
async def test_message_kind_from_client_cannot_be_forged(env) -> None:
    """Роут отправки всегда пишет 'human' — 'ai' ставит только сервер."""
    client, _db, _owner_user, anya, borya, _vika = env
    thread_id = await _befriend(anya["id"], borya["id"])

    await _as(client, anya["id"])
    r = await client.post(
        f"/api/messages/{thread_id}/send", json={"body": "я не ИИ", "kind": "ai"}
    )
    assert r.json()["message"]["kind"] == "human"


# ── E. Страницы ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_friends_page_lists_requests_and_friends_without_emails(env) -> None:
    client, _db, _owner_user, anya, borya, vika = env
    await _befriend(anya["id"], borya["id"])
    await social.send_request(vika["id"], anya["id"], "будем знакомы")

    await _as(client, anya["id"])
    page = await client.get("/friends")
    assert page.status_code == 200
    assert "Боря" in page.text
    assert "будем знакомы" in page.text
    assert "v***@social.test" in page.text
    # Ни одного настоящего адреса на странице.
    for email in ("borya@social.test", "vika@social.test", "owner@social.test"):
        assert email not in page.text, email


@pytest.mark.asyncio
async def test_messages_page_shows_unread_badge_and_preview(env) -> None:
    client, _db, _owner_user, anya, borya, _vika = env
    thread_id = await _befriend(anya["id"], borya["id"])
    await social.send_message(thread_id, anya["id"], "превью сообщения")

    await _as(client, borya["id"])
    page = await client.get("/messages")
    assert page.status_code == 200
    assert "превью сообщения" in page.text
    assert "Аня" in page.text


@pytest.mark.asyncio
async def test_discoverable_toggle_round_trips(env) -> None:
    client, _db, _owner_user, anya, _borya, _vika = env
    await _as(client, anya["id"])
    assert await social.is_discoverable(anya["id"]) is True

    r = await client.post(
        "/api/friends/discoverable",
        json={"value": False},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["discoverable"] is False
    assert await social.is_discoverable(anya["id"]) is False


@pytest.mark.asyncio
async def test_social_paths_are_member_surface(env) -> None:
    """Гейт обязан пускать участника в /friends и /messages."""
    for path in ("/friends", "/messages", "/api/friends/search", "/api/messages/unread.json"):
        assert auth_gate._is_member_path(path) is True, path
    # …но соседние пути не проваливаются по префиксу.
    assert auth_gate._is_member_path("/friendsXXX") is False
    assert auth_gate._is_member_path("/messagesXXX") is False
