"""Тесты авторегистрации по почте + гард опечаток доменов (лендинг)."""

from __future__ import annotations

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
async def test_register_creates_account_emails_password_logs_in(client):
    ac, sent = client
    r = await ac.post(
        "/auth/register",
        data={"email": "newuser@example.io"},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["registered"] and body["delivered"]
    assert body["redirect"].startswith("/")
    # залогинен — выставлена сессионная кука
    assert r.cookies.get("persona_session")
    # пароль ушёл на почту
    assert sent and sent[0]["to"] == "newuser@example.io"
    assert "Пароль" in sent[0]["text"]


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
