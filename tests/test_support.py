"""Поддержка: публичная форма, ящик владельца, почта и её отсутствие.

Что здесь проверяется и почему именно это
-----------------------------------------
Фича обещает одну вещь: «любой может написать владельцу, и это до него
дойдёт». Значит тесты обязаны держать три инварианта:

1. **Дойдёт.** Обращение сохраняется даже когда почта сломана (а на этом
   инстансе она сломана: ``smtp_enabled='true'`` при пустом ``smtp_host``),
   и обращение честно помнит, что письма не было.
2. **Может любой.** Аноним отправляет с адресом и НЕ отправляет без него;
   залогиненному обращение привязывается к аккаунту.
3. **Не любой ЧИТАЕТ.** Ящик — только владельцу. Проверка канареечная (как в
   ``tests/test_member_data_isolation_audit.py``): в HTML участника не должно
   быть ни темы, ни текста, ни email автора.

Плюс анти-абуз (ловушка, время на форме, лимит, потолок длины) — это
публичная ручка записи, и её отказы должны быть ЧЕСТНЫМИ, а не «спасибо»
в лицо боту.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner as owner_mod
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.support import repository as support_repo
from app.web import rate_limit, templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.routes import setup_gate

pytestmark = pytest.mark.asyncio

# Канарейки: строки, которых нет ни в шаблонах, ни в переводах. Любое
# совпадение в чужом ответе — настоящая утечка, а не ложная тревога.
C_SUBJECT = "KANAREYKA-SUPPORT-SUBJ-SP01"
C_BODY = "KANAREYKA-SUPPORT-BODY-SP02"
C_EMAIL = "sp03-kanareyka@support-audit.test"

OWNER_EMAIL = "owner@support-audit.test"
MEMBER_EMAIL = "member@support-audit.test"


def _reset_caches() -> None:
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
    rate_limit._EVENTS.clear()
    # Соль инстансная и кэшируется в процессе, а БД у каждого теста своя:
    # без сброса подпись формы, выданная в прошлом тесте, не сойдётся здесь.
    support_repo.reset_salt_cache()


@pytest_asyncio.fixture
async def env(monkeypatch):
    """Приложение + владелец + участник. Почта — заведомо НЕработающая.

    Пустых kv-строк для этого НЕ ХВАТАЕТ, и это не мелочь теста, а поведение
    продукта: :func:`app.smtp_delivery._load_settings` при пустом kv берёт
    значение из ``.env`` (``PERSONA_SMTP_*``), а ``smtp_from`` доводит из
    ``smtp_user``. В репозитории такой ``.env`` есть, поэтому «пустой kv»
    означает НЕ «почта выключена», а «почта настроена на gmail» — и тест
    молча уходил бы в реальный TCP на 587-й порт (16 с на отказ).
    Глушим оба источника разом.
    """
    from app.settings import get_settings

    for key in ("HOST", "PORT", "USER", "PASS", "FROM", "TO", "TLS"):
        monkeypatch.setenv(f"PERSONA_SMTP_{key}", "")
    monkeypatch.setenv("PERSONA_SMTP_ENABLED", "true")
    get_settings.cache_clear()

    await init_database()
    owner = await create_user(OWNER_EMAIL, "Zx7-Alpha-Passphrase")
    member = await create_user(MEMBER_EMAIL, "Qw4-Bravo-Passphrase")
    async with get_connection() as conn:
        await set_kv(conn, "owner_user_id", str(owner["id"]))
        # ИМЕННО ЭТА конфигурация живёт на инстансе: тумблер включён, релея нет.
        await set_kv(conn, "smtp_enabled", "true")
        await set_kv(conn, "smtp_host", "")
        await set_kv(conn, "smtp_from", "")
    setup_gate._cache.mark_done()
    _reset_caches()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield {"client": client, "owner": owner, "member": member}
    finally:
        _reset_caches()
        get_settings.cache_clear()


async def _as(client: AsyncClient, uid: int | None) -> None:
    client.cookies.clear()
    if uid is not None:
        token, _ = await issue_session(uid)
        client.cookies.set(SESSION_COOKIE_NAME, token)


async def _fresh_ts(seconds_ago: int = 30) -> str:
    """Подпись формы, выданная ``seconds_ago`` секунд назад.

    Тесты не могут ждать реальные 4 секунды на каждой отправке, а подделать
    поле нельзя — оно подписано инстансной солью. Поэтому просим подпись на
    прошлое время у того же кода, что её проверяет.
    """
    return await support_repo.sign_form_ts(time.time() - seconds_ago)


async def _submit(client: AsyncClient, **overrides: Any):
    data = {
        "ts": await _fresh_ts(),
        "subject": C_SUBJECT,
        "body": C_BODY,
        "email": C_EMAIL,
        "website": "",
        "from": "/chat",
    }
    data.update(overrides)
    return await client.post("/support", data=data)


# ── Публичная форма ─────────────────────────────────────────────────────────


async def test_anonymous_sees_the_form_with_csrf_field(env) -> None:
    """Форма открыта анониму, и в ней есть поле CSRF-токена.

    Для анонима ``csrf_input`` намеренно отдаёт пустую строку (защищать нечего:
    нет сессии — нет ambient authority), поэтому проверяем ОБА случая: у
    анонима форма просто существует, а у залогиненного в ней есть hidden-поле.
    """
    client = env["client"]
    await _as(client, None)
    response = await client.get("/support")
    assert response.status_code == 200
    assert '<form method="post" action="/support"' in response.text

    await _as(client, env["member"]["id"])
    response = await client.get("/support")
    assert response.status_code == 200
    assert 'name="csrf_token"' in response.text


async def test_anonymous_can_submit_with_email(env) -> None:
    client = env["client"]
    await _as(client, None)
    response = await _submit(client)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/support?sent=")

    tickets = await support_repo.list_tickets()
    assert len(tickets) == 1
    assert tickets[0]["subject"] == C_SUBJECT
    assert tickets[0]["email"] == C_EMAIL
    assert tickets[0]["user_id"] is None
    assert tickets[0]["role"] == "anon"


async def test_anonymous_cannot_submit_without_email(env) -> None:
    """Отказ ЧЕСТНЫЙ: 400 и внятная причина, а не фальшивое «спасибо»."""
    client = env["client"]
    await _as(client, None)
    response = await _submit(client, email="")
    assert response.status_code == 400
    assert "email" in response.text.lower()
    # И — главное — обращения не появилось.
    assert await support_repo.list_tickets() == []


async def test_logged_in_submit_attaches_user_id_and_account_email(env) -> None:
    """У залогиненного адрес берётся ИЗ АККАУНТА, а не из присланного поля."""
    client = env["client"]
    await _as(client, env["member"]["id"])
    response = await _submit(client, email="podmena@evil.test")
    assert response.status_code == 303

    tickets = await support_repo.list_tickets()
    assert len(tickets) == 1
    assert tickets[0]["user_id"] == env["member"]["id"]
    assert tickets[0]["email"] == MEMBER_EMAIL
    assert tickets[0]["role"] == "member"


async def test_honeypot_rejects(env) -> None:
    client = env["client"]
    await _as(client, None)
    response = await _submit(client, website="http://spam.example")
    assert response.status_code == 400
    assert await support_repo.list_tickets() == []


async def test_too_fast_submit_rejects(env) -> None:
    """Форма отправлена мгновенно после выдачи — так делают боты."""
    client = env["client"]
    await _as(client, None)
    response = await _submit(client, ts=await support_repo.sign_form_ts())
    assert response.status_code == 400
    assert await support_repo.list_tickets() == []


async def test_forged_form_timestamp_rejects(env) -> None:
    """Неподписанное число в поле ``ts`` не проходит: подпись на соли инстанса."""
    client = env["client"]
    await _as(client, None)
    response = await _submit(client, ts=str(int(time.time() - 300)))
    assert response.status_code == 400
    assert await support_repo.list_tickets() == []


async def test_oversized_body_rejected(env) -> None:
    from app.support import service  # локально: тест про конкретный потолок

    client = env["client"]
    await _as(client, None)
    response = await _submit(client, body="я" * (service.BODY_MAX + 50))
    assert response.status_code == 400
    assert str(service.BODY_MAX) in response.text
    assert await support_repo.list_tickets() == []


async def test_rate_limit_rejects_burst(env) -> None:
    """Третья отправка подряд с одного адреса отбивается 429, а не 500/303."""
    client = env["client"]
    await _as(client, None)
    codes = []
    for _ in range(4):
        codes.append((await _submit(client)).status_code)
    assert codes[:2] == [303, 303]
    assert 429 in codes[2:]
    # Отбитые отправки не создали обращений.
    assert len(await support_repo.list_tickets()) == 2


# ── Почта: сломана и починена ───────────────────────────────────────────────


async def test_submit_succeeds_when_smtp_is_broken_and_records_it(env) -> None:
    """Главный инвариант: сломанная почта НЕ мешает обращению дойти до сайта."""
    client = env["client"]
    await _as(client, None)
    response = await _submit(client)
    assert response.status_code == 303

    ticket = (await support_repo.list_tickets())[0]
    assert ticket["owner_notify_status"].startswith("skipped:")
    assert "misconfigured" in ticket["owner_notify_status"]
    assert ticket["owner_notified_at"]


async def test_submit_succeeds_when_the_relay_refuses(env, monkeypatch) -> None:
    """Второй реальный сценарий: конфиг ЕСТЬ, но релей не пускает.

    Именно так ведёт себя боевой сервер: ``.env`` отдаёт gmail, поэтому
    ``delivery_status()`` честно говорит ``'ok'``, а исходящий 587 закрыт и
    ``aiosmtplib`` отвечает отказом. Посетитель обязан этого не заметить.
    """

    async def fake_status() -> str:
        return "ok"

    async def fake_send(to_addr, subject, body_text, body_html=None):
        return {"status": "error", "error": "connection refused"}

    import app.smtp_delivery as smtp

    monkeypatch.setattr(smtp, "delivery_status", fake_status)
    monkeypatch.setattr(smtp, "send_email", fake_send)

    client = env["client"]
    await _as(client, None)
    assert (await _submit(client)).status_code == 303

    ticket = (await support_repo.list_tickets())[0]
    assert ticket["subject"] == C_SUBJECT
    assert ticket["owner_notify_status"].startswith("error:")


async def test_hung_relay_does_not_hang_the_ticket(env, monkeypatch) -> None:
    """Зависший релей превращается в записанный таймаут, а не в вечное ожидание."""
    import asyncio

    async def fake_status() -> str:
        return "ok"

    async def fake_send(to_addr, subject, body_text, body_html=None):
        await asyncio.sleep(60)
        return {"status": "sent", "to": to_addr}

    import app.smtp_delivery as smtp
    from app.support import notify as notify_mod

    monkeypatch.setattr(smtp, "delivery_status", fake_status)
    monkeypatch.setattr(smtp, "send_email", fake_send)
    monkeypatch.setattr(notify_mod, "_SEND_TIMEOUT", 0.2)

    client = env["client"]
    await _as(client, None)
    assert (await _submit(client)).status_code == 303

    ticket = (await support_repo.list_tickets())[0]
    assert "с" in ticket["owner_notify_status"]
    assert ticket["owner_notify_status"].startswith("error:")


async def test_working_smtp_mails_the_owner_with_the_ticket(env, monkeypatch) -> None:
    """При рабочей почте владелец получает письмо, и в нём есть текст обращения."""
    sent: list[dict[str, Any]] = []

    async def fake_status() -> str:
        return "ok"

    async def fake_send(to_addr, subject, body_text, body_html=None):
        sent.append({"to": to_addr, "subject": subject, "body": body_text})
        return {"status": "sent", "to": to_addr}

    import app.smtp_delivery as smtp

    monkeypatch.setattr(smtp, "delivery_status", fake_status)
    monkeypatch.setattr(smtp, "send_email", fake_send)

    client = env["client"]
    await _as(client, None)
    assert (await _submit(client)).status_code == 303

    assert len(sent) == 1
    assert sent[0]["to"] == OWNER_EMAIL  # адрес прочитан ИЗ БД, не захардкожен
    assert C_SUBJECT in sent[0]["subject"]
    assert C_BODY in sent[0]["body"]
    assert C_EMAIL in sent[0]["body"]

    ticket = (await support_repo.list_tickets())[0]
    assert ticket["owner_notify_status"] == "sent"


async def test_owner_reply_mails_the_author(env, monkeypatch) -> None:
    sent: list[dict[str, Any]] = []

    async def fake_status() -> str:
        return "ok"

    async def fake_send(to_addr, subject, body_text, body_html=None):
        sent.append({"to": to_addr, "subject": subject, "body": body_text})
        return {"status": "sent", "to": to_addr}

    import app.smtp_delivery as smtp

    monkeypatch.setattr(smtp, "delivery_status", fake_status)
    monkeypatch.setattr(smtp, "send_email", fake_send)

    client = env["client"]
    await _as(client, None)
    await _submit(client)
    ticket_id = (await support_repo.list_tickets())[0]["id"]
    sent.clear()

    await _as(client, env["owner"]["id"])
    response = await client.post(
        "/settings/support",
        data={"ticket_id": str(ticket_id), "action": "reply", "reply": "Починил, спасибо."},
    )
    assert response.status_code == 303

    assert len(sent) == 1
    assert sent[0]["to"] == C_EMAIL
    assert "Починил" in sent[0]["body"]

    messages = await support_repo.list_messages(ticket_id)
    assert len(messages) == 1
    assert messages[0]["delivery_status"] == "sent"
    # Ответ переводит обращение в 'answered' автоматически.
    assert (await support_repo.get_ticket(ticket_id))["status"] == "answered"


async def test_reply_is_stored_even_when_mail_is_dead(env) -> None:
    """Почта сломана — ответ всё равно сохранён, и это видно на сообщении."""
    client = env["client"]
    await _as(client, None)
    await _submit(client)
    ticket_id = (await support_repo.list_tickets())[0]["id"]

    await _as(client, env["owner"]["id"])
    response = await client.post(
        "/settings/support",
        data={"ticket_id": str(ticket_id), "action": "reply", "reply": "Ответ без почты."},
    )
    assert response.status_code == 303

    messages = await support_repo.list_messages(ticket_id)
    assert len(messages) == 1
    assert messages[0]["body"] == "Ответ без почты."
    assert messages[0]["delivery_status"].startswith("skipped:")


# ── Ящик владельца ──────────────────────────────────────────────────────────


async def test_ticket_appears_in_owner_inbox(env) -> None:
    client = env["client"]
    await _as(client, None)
    await _submit(client)

    await _as(client, env["owner"]["id"])
    response = await client.get("/settings/support")
    assert response.status_code == 200
    assert C_SUBJECT in response.text

    ticket_id = (await support_repo.list_tickets())[0]["id"]
    response = await client.get(f"/settings/support?ticket={ticket_id}")
    assert response.status_code == 200
    assert C_BODY in response.text
    assert C_EMAIL in response.text
    # Открытие двигает 'new' → 'read' и только его.
    assert (await support_repo.get_ticket(ticket_id))["status"] == "read"


async def test_inbox_is_invisible_to_a_member(env) -> None:
    """Участник не видит ящик — ни страницу, ни JSON-бейдж, ни канарейки."""
    client = env["client"]
    await _as(client, None)
    await _submit(client)
    ticket_id = (await support_repo.list_tickets())[0]["id"]

    await _as(client, env["member"]["id"])
    for url in (
        "/settings/support",
        f"/settings/support?ticket={ticket_id}",
        "/api/support/unread.json",
    ):
        response = await client.get(url, follow_redirects=False)
        assert response.status_code in (303, 403), url
        for canary in (C_SUBJECT, C_BODY, C_EMAIL):
            assert canary not in response.text, f"утечка {canary} в {url}"

    # И POST-действие тоже недостижимо: чужой ящик нельзя даже перевести
    # в другой статус вслепую.
    response = await client.post(
        "/settings/support",
        data={"ticket_id": str(ticket_id), "action": "closed"},
        follow_redirects=False,
    )
    assert response.status_code in (303, 403)
    assert (await support_repo.get_ticket(ticket_id))["status"] == "new"


async def test_inbox_is_invisible_to_anonymous(env) -> None:
    client = env["client"]
    await _as(client, None)
    await _submit(client)

    response = await client.get("/settings/support", follow_redirects=False)
    assert response.status_code in (303, 401, 403)
    assert C_SUBJECT not in response.text

    response = await client.get("/api/support/unread.json", follow_redirects=False)
    assert response.status_code in (303, 401, 403)


async def test_status_transitions_and_filter(env) -> None:
    client = env["client"]
    await _as(client, None)
    await _submit(client)
    ticket_id = (await support_repo.list_tickets())[0]["id"]

    await _as(client, env["owner"]["id"])
    for status in ("read", "answered", "closed", "new"):
        response = await client.post(
            "/settings/support",
            data={"ticket_id": str(ticket_id), "action": status},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert (await support_repo.get_ticket(ticket_id))["status"] == status

    # Фильтр по статусу показывает и не показывает то, что должен.
    await support_repo.set_status(ticket_id, "closed")
    response = await client.get("/settings/support?status=closed")
    assert C_SUBJECT in response.text
    response = await client.get("/settings/support?status=new")
    assert C_SUBJECT not in response.text

    # Неизвестный статус не проходит ни через роут, ни через репозиторий.
    assert await support_repo.set_status(ticket_id, "voobshe-drugoy") is False


async def test_unread_badge_json_for_owner(env) -> None:
    client = env["client"]
    await _as(client, None)
    await _submit(client)

    await _as(client, env["owner"]["id"])
    response = await client.get("/api/support/unread.json")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["new"] == 1
    assert payload["url"] == "/settings/support"


async def test_owner_can_delete_a_ticket_with_its_thread(env) -> None:
    """Обещание под формой («хранится, пока владелец не удалит») исполнимо."""
    client = env["client"]
    await _as(client, None)
    await _submit(client)
    ticket_id = (await support_repo.list_tickets())[0]["id"]

    await _as(client, env["owner"]["id"])
    await client.post(
        "/settings/support",
        data={"ticket_id": str(ticket_id), "action": "reply", "reply": "текст"},
    )
    assert len(await support_repo.list_messages(ticket_id)) == 1

    response = await client.post(
        "/settings/support",
        data={"ticket_id": str(ticket_id), "action": "delete"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert await support_repo.get_ticket(ticket_id) is None
    # Каскад: переписка ушла вместе с обращением.
    assert await support_repo.list_messages(ticket_id) == []


# ── Приватность контекста ───────────────────────────────────────────────────


async def test_raw_ip_and_user_agent_are_not_stored(env) -> None:
    """Хранится КЛАСС браузера и НЕОБРАТИМЫЙ хэш адреса, а не сырые значения."""
    ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Chrome/122.0.0.0 Safari/537.36"
    client = env["client"]
    await _as(client, None)
    response = await client.post(
        "/support",
        data={
            "ts": await _fresh_ts(),
            "subject": C_SUBJECT,
            "body": C_BODY,
            "email": C_EMAIL,
            "website": "",
            "from": "/timeline?q=sekret&token=abc",
        },
        headers={"user-agent": ua},
    )
    assert response.status_code == 303

    ticket = (await support_repo.list_tickets())[0]
    assert ticket["browser_class"] == "Chrome · мобильный"
    assert ua not in str(ticket)
    # query-string с токеном отрезана — в контексте только путь.
    assert ticket["source_page"] == "/timeline"
    # Хэш есть, он короткий и не равен ни одному из известных представлений IP.
    assert len(ticket["ip_hash"]) == 16
    assert "127.0.0.1" not in ticket["ip_hash"]


async def test_ip_hash_is_stable_and_salted(env) -> None:
    """Один адрес → один хэш (иначе он бесполезен); соль инстансная."""
    first = await support_repo.hash_ip("203.0.113.7")
    second = await support_repo.hash_ip("203.0.113.7")
    other = await support_repo.hash_ip("203.0.113.8")
    assert first == second
    assert first != other
    assert first != ""
    assert await support_repo.hash_ip("") == ""


async def test_public_prefix_covers_support_but_not_the_inbox(env) -> None:
    """Гейт: /support публичен, /settings/support — нет. Совпадение префиксное."""
    assert auth_gate._is_public_path("/support") is True
    assert auth_gate._is_public_path("/settings/support") is False
    assert auth_gate._is_member_path("/settings/support") is False
