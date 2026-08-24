"""Тесты авторегистрации по почте + гард опечаток доменов (лендинг)."""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.email_check import check_email
from app.storage.db import init_database
from app.web.routes import auth as auth_routes


# --------------------------------------------------------------- email_check

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("user@gmail.ru", "user@gmail.com"),      # классическая опечатка из запроса
        ("user@gmail.xyz", "user@gmail.com"),     # canonical-правило: любой gmail.* ≠ .com
        ("user@gmail.co", "user@gmail.com"),
        ("user@gmial.com", "user@gmail.com"),     # перестановка букв
        ("user@icloud.ru", "user@icloud.com"),
        ("user@yandex.com", "user@yandex.ru"),
        ("user@gmail.com", None),                 # корректный — без подсказки
        ("user@icloud.com", None),                # корректный (регресс на мой баг self-map)
        ("user@yandex.ru", None),
        ("user@mycompany.io", None),              # незнакомый домен — не трогаем
    ],
)
def test_email_typo_suggestions(raw, expected):
    assert check_email(raw)["suggestion"] == expected


def test_email_format_validation():
    assert check_email("not-an-email")["valid"] is False
    assert check_email("a@b.co")["valid"] is True


# --------------------------------------------------------------- /auth/register

@pytest_asyncio.fixture
async def client(monkeypatch):
    sent: list[dict] = []

    async def _fake_send(to_addr, subject, body_text, body_html=None):
        sent.append({"to": to_addr, "subject": subject, "text": body_text})
        return {"status": "sent", "to": to_addr}

    monkeypatch.setattr(auth_routes, "send_email", _fake_send)
    monkeypatch.setattr(auth_routes, "_rate_allow", lambda *a, **k: True)  # без троттлинга в тестах
    await init_database()
    app = FastAPI()
    app.include_router(auth_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, sent


@pytest.mark.asyncio
async def test_register_creates_account_shows_password_and_logs_in(client):
    """Пароль ВСЕГДА возвращается вызывающему, письмо — только дубль.

    Раньше пароль уходил исключительно письмом (а ``aiosmtplib`` не был
    установлен → письмо не уходило никогда) — зарегистрировавшийся получал
    аккаунт, в который не мог войти повторно.
    """
    ac, sent = client
    r = await ac.post(
        "/auth/register",
        data={"email": "newuser@example.io"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["registered"] and body["delivered"]
    # пароль показан прямо в ответе, а не спрятан в письме
    assert body["password"] and len(body["password"]) >= 8
    assert body["next"].startswith("/")
    assert body["set_password_url"] == "/auth/set-password"
    # автопереход убран: лендинг не должен увести человека до показа пароля
    assert "redirect" not in body
    # залогинен — выставлена сессионная кука
    assert r.cookies.get("persona_session")
    # пароль ушёл и на почту (дубль)
    assert sent and sent[0]["to"] == "newuser@example.io"
    assert body["password"] in sent[0]["text"]


@pytest.mark.asyncio
async def test_register_shows_password_on_page_when_smtp_absent(monkeypatch):
    """SMTP не настроен → страница ЧЕСТНО говорит это и печатает пароль.

    Ключевой инвариант блокера №1: экранный показ безусловен. Никакого
    «письмо отправлено» при выключенной почте и никакого молчаливого редиректа
    в приложение (после которого пароль потерян навсегда).
    """
    async def _no_smtp(to_addr, subject, body_text, body_html=None):
        return {"status": "disabled"}

    monkeypatch.setattr(auth_routes, "send_email", _no_smtp)
    monkeypatch.setattr(auth_routes, "_rate_allow", lambda *a, **k: True)
    await init_database()
    app = FastAPI()
    app.include_router(auth_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # обычный form-post (без JS) — путь, по которому идёт браузер без JS
        r = await ac.post(
            "/auth/register", data={"email": "nosmtp@example.io"}, follow_redirects=False
        )
        assert r.status_code == 200, r.status_code  # НЕ 303 в приложение
        page = r.text
        assert "Аккаунт создан" in page
        assert "Сохрани его" in page
        assert "/auth/set-password" in page
        # честность про почту
        assert "не настроена" in page
        assert "отправили письмом" not in page
        assert r.cookies.get("persona_session")

        # на странице действительно есть рабочий пароль: им можно войти
        match = re.search(r'id="pw-value"[^>]*>([^<]+)<', page)
        assert match, "пароль не найден на странице"
        password = match.group(1).strip()
        assert len(password) >= 8

        ac.cookies.clear()
        login = await ac.post(
            "/auth/login",
            data={"email": "nosmtp@example.io", "password": password},
            headers={"X-Requested-With": "fetch"},
        )
        assert login.status_code == 200 and login.json()["ok"] is True


@pytest.mark.asyncio
async def test_register_survives_a_crashing_mailer(monkeypatch):
    """Падение почты не должно давать 500 — аккаунт создаётся, пароль виден."""
    async def _boom(to_addr, subject, body_text, body_html=None):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(auth_routes, "send_email", _boom)
    monkeypatch.setattr(auth_routes, "_rate_allow", lambda *a, **k: True)
    await init_database()
    app = FastAPI()
    app.include_router(auth_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/auth/register",
            data={"email": "boom@example.io"},
            headers={"X-Requested-With": "fetch"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["delivered"] is False and body["password"]


@pytest.mark.asyncio
async def test_register_does_not_start_a_trial(client):
    """Биллинг спит: регистрация НЕ заводит подписку/триал."""
    from app.billing import service as billing_service

    ac, _sent = client
    await ac.post(
        "/auth/register",
        data={"email": "notrial@example.io"},
        headers={"X-Requested-With": "fetch"},
    )
    uid = await auth_routes._user_id_for_email("notrial@example.io")
    assert uid is not None
    summary = await billing_service.summary(uid)
    assert summary["active"] is False
    assert summary["plan"] == "free"
    assert summary["license_key"] is None


@pytest.mark.asyncio
async def test_register_blocks_typo_domain(client):
    ac, sent = client
    r = await ac.post(
        "/auth/register",
        data={"email": "me@gmail.ru"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False and body["suggestion"] == "me@gmail.com"
    assert sent == []  # аккаунт не создан, письмо не ушло


@pytest.mark.asyncio
async def test_register_existing_sends_login_link(client):
    ac, sent = client
    await ac.post("/auth/register", data={"email": "dup@example.io"},
                  headers={"X-Requested-With": "fetch"})
    sent.clear()
    r = await ac.post("/auth/register", data={"email": "dup@example.io"},
                      headers={"X-Requested-With": "fetch"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body.get("existing") is True
    # отправлена ссылка для входа, аккаунт не пересоздан
    assert sent and sent[0]["to"] == "dup@example.io"
