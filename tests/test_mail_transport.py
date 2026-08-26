"""Транспорт почты: выбор, отказы, честность статуса, потолки, секреты.

ЧТО ЭТИ ТЕСТЫ ЗАЩИЩАЮТ (все три пункта — реальные дефекты, а не гипотезы):

1. **``delivery_status()`` врал.** Он отвечал ``'ok'`` за «поля заполнены», а
   не за «письмо уйдёт». На этом сервере исходящие 25/465/587/2525 закрыты
   файрволом по порту, поэтому «ok» означало 16 секунд ожидания и WinError
   1225. Теперь ``ok`` требует ещё и TCP-достижимости, и тесты держат именно
   это: недостижимый транспорт обязан называться ``unreachable``.
2. **Почта не имеет права уронить запрос.** Ни один транспорт не бросает
   наружу — ни на отказе соединения, ни на HTTP-500, ни на зависании.
3. **Ключ провайдера не утекает.** Ни в статус-словарь, ни в лог — даже когда
   провайдер вернул его в тексте ошибки.

**Сеть здесь не используется.** ``PERSONA_MAIL_PROBE=0`` стоит глобально в
``tests/conftest.py``; тесты, которым нужна проба, включают её сами и
подменяют сокет/HTTP-слой.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import mail_transport as mt
from app.storage.db import init_database
from app.storage.repository import set_kv
from app.web.routes import auth as auth_routes

FAKE_KEY = "re_TESTKEY_neverInAnyLogOrStatus_0123456789"
FAKE_PASS = "app-password-must-not-leak-4242"


@pytest.fixture(autouse=True)
def _clean_transport_state(monkeypatch: pytest.MonkeyPatch):
    """Каждый тест начинает с пустым кэшем проб и без ключа в окружении."""
    mt.reset_probe_cache()
    monkeypatch.delenv(mt.API_KEY_ENV, raising=False)
    monkeypatch.delenv("PERSONA_MAIL_TIMEOUT", raising=False)
    # ПУСТАЯ строка, а не delenv: ``.env`` разработчика читается pydantic'ом с
    # диска и содержит боевой gmail-релей. Только явно заданная пустая
    # переменная окружения перебивает файл — иначе тест про «ничего не
    # настроено» молча тестировал бы чужой конфиг.
    for key in ("PERSONA_SMTP_ENABLED", "PERSONA_SMTP_HOST", "PERSONA_SMTP_PORT",
                "PERSONA_SMTP_USER", "PERSONA_SMTP_PASS", "PERSONA_SMTP_TO",
                "PERSONA_SMTP_FROM", "PERSONA_SMTP_TLS", "PERSONA_MAIL_TRANSPORT"):
        monkeypatch.setenv(key, "")
    from app.settings import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    mt.reset_probe_cache()
    get_settings.cache_clear()  # type: ignore[attr-defined]


# ── выбор транспорта из конфига ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", mt.SMTP_STARTTLS),          # пусто = сегодняшнее поведение
        (None, mt.SMTP_STARTTLS),
        ("smtp_starttls", mt.SMTP_STARTTLS),
        ("smtp_ssl", mt.SMTP_SSL),
        ("SMTP-SSL", mt.SMTP_SSL),       # регистр и дефис — человеческий ввод
        ("http_api", mt.HTTP_API),
        ("resend", mt.HTTP_API),
        ("465", mt.SMTP_SSL),
        ("совершенно не транспорт", mt.SMTP_STARTTLS),
    ],
)
def test_transport_is_resolved_from_config(raw, expected) -> None:
    """Опечатка в настройке НЕ выключает почту — она оставляет дефолт."""
    assert mt.resolve_transport(raw) == expected


@pytest.mark.asyncio
async def test_kv_beats_env_when_choosing_the_transport(db, monkeypatch) -> None:
    """Правило проекта («kv выигрывает, env — дефолт») действует и для транспорта."""
    from app.settings import get_settings
    from app.smtp_delivery import _build_config, _load_settings

    monkeypatch.setenv("PERSONA_MAIL_TRANSPORT", "smtp_ssl")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert _build_config(await _load_settings()).transport == mt.SMTP_SSL

    await set_kv(db, "mail_transport", "http_api")
    assert _build_config(await _load_settings()).transport == mt.HTTP_API


@pytest.mark.asyncio
async def test_each_transport_reaches_for_its_own_endpoint(db) -> None:
    """SMTP идёт на свой релей, http_api — на 443 провайдера, а не на smtp_host."""
    from app.smtp_delivery import _build_config, _load_settings

    await set_kv(db, "smtp_host", "relay.example.test")
    await set_kv(db, "smtp_port", "2465")
    await set_kv(db, "mail_transport", "smtp_ssl")
    assert mt.endpoint(_build_config(await _load_settings())) == ("relay.example.test", 2465)

    await set_kv(db, "mail_transport", "http_api")
    assert mt.endpoint(_build_config(await _load_settings())) == (mt.HTTP_API_HOST, 443)


# ── каждый транспорт: отказ ловится и НИКОГДА не летит наружу ───────────────


def _cfg(transport: str, **kw) -> mt.MailConfig:
    base = {"sender": "persona@example.test", "timeout": 2.0, "password": FAKE_PASS}
    if transport == mt.HTTP_API:
        base["api_key"] = FAKE_KEY
    else:
        base |= {"host": "relay.example.test", "port": 587}
    return mt.MailConfig(transport=transport, **(base | kw))


def _msg() -> mt.MailMessage:
    return mt.MailMessage(recipient="member@example.test", subject="Тема", text="Тело")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [mt.SMTP_STARTTLS, mt.SMTP_SSL])
@pytest.mark.parametrize(
    "boom",
    [
        ConnectionRefusedError("[WinError 1225] удалённый компьютер отверг соединение"),
        OSError("[WinError 10061] target machine actively refused it"),
        RuntimeError("релей внезапно передумал"),
    ],
)
async def test_smtp_failures_are_reported_never_raised(monkeypatch, transport, boom) -> None:
    """Ровно те исключения, что видел прод, — и ни одно не долетает до запроса."""
    async def _boom(*_a, **_k):
        raise boom

    monkeypatch.setattr(mt, "_send_smtp", _boom)
    result = await mt.deliver(_cfg(transport), _msg())
    assert result["status"] == "error"
    assert result["error"]


@pytest.mark.asyncio
async def test_http_api_failure_is_reported_never_raised(monkeypatch) -> None:
    """Провайдер лежит / сеть моргнула — статус-словарь, а не исключение."""
    async def _boom(*_a, **_k):
        raise OSError("TLS handshake failed")

    monkeypatch.setattr(mt, "_send_http_api", _boom)
    result = await mt.deliver(_cfg(mt.HTTP_API), _msg())
    assert result["status"] == "error"
    assert "TLS handshake failed" in str(result["error"])


@pytest.mark.asyncio
async def test_http_api_rejection_is_translated_not_raised(monkeypatch) -> None:
    """HTTP 401/422 — это отказ провайдера, а не авария: читаемый error-статус."""

    class _Response:
        status_code = 401

        @staticmethod
        def json() -> dict[str, str]:
            return {"statusCode": 401, "message": "API key is invalid", "name": "validation_error"}

        text = '{"message": "API key is invalid"}'

    class _Client:
        def __init__(self, **_kw) -> None: ...
        async def __aenter__(self): return self
        async def __aexit__(self, *_exc): return False
        async def post(self, *_a, **_k): return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    result = await mt.deliver(_cfg(mt.HTTP_API), _msg())
    assert result["status"] == "error"
    assert "401" in str(result["error"])
    assert "API key is invalid" in str(result["error"])


@pytest.mark.asyncio
async def test_http_api_success_returns_the_provider_message_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, str]:
            return {"id": "0f4a-..."}

    class _Client:
        def __init__(self, **kw) -> None:
            captured["timeout"] = kw.get("timeout")

        async def __aenter__(self): return self
        async def __aexit__(self, *_exc): return False

        async def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    msg = mt.MailMessage(
        recipient="member@example.test", subject="Пароль", text="plain",
        html="<b>rich</b>", attachments=[("stats.csv", b"a,b\n1,2\n", "text/csv")],
    )
    result = await mt.deliver(_cfg(mt.HTTP_API), msg)

    assert result["status"] == "sent"
    assert result["message_id"] == "0f4a-..."
    assert captured["url"] == mt.HTTP_API_URL
    payload = captured["json"]
    assert payload["to"] == ["member@example.test"]
    assert payload["from"] == "persona@example.test"
    assert payload["text"] == "plain" and payload["html"] == "<b>rich</b>"
    # Вложение обязано доехать base64, иначе недельный CSV молча теряется.
    assert payload["attachments"][0]["filename"] == "stats.csv"
    assert payload["attachments"][0]["content"] == "YSxiCjEsMgo="
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


# ── потолки времени ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [mt.SMTP_STARTTLS, mt.SMTP_SSL, mt.HTTP_API])
async def test_a_hanging_transport_is_cut_off_at_the_timeout(monkeypatch, transport) -> None:
    """Тот самый 16-секундный вис. Потолок накрывает ЛЮБОЙ транспорт."""
    async def _hang(*_a, **_k):
        await asyncio.sleep(30)

    monkeypatch.setattr(mt, "_send_smtp", _hang)
    monkeypatch.setattr(mt, "_send_http_api", _hang)

    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await mt.deliver(_cfg(transport, timeout=1.0), _msg())
    elapsed = loop.time() - started

    assert result["status"] == "timeout"
    assert elapsed < 5.0, f"потолок не сработал: ждали {elapsed:.1f} с"


def test_the_timeout_is_clamped_to_something_a_visitor_can_survive() -> None:
    assert mt.resolve_timeout("") == mt.DEFAULT_TIMEOUT
    assert mt.resolve_timeout("не число") == mt.DEFAULT_TIMEOUT
    assert mt.resolve_timeout("0.01") == 1.0        # никакого «отправка за 10 мс»
    assert mt.resolve_timeout("900") == 60.0        # и никакого «повисим 15 минут»
    assert mt.resolve_timeout("4.5") == 4.5


@pytest.mark.asyncio
async def test_the_probe_has_its_own_shorter_ceiling(monkeypatch) -> None:
    """Проба живёт на пути отрисовки страницы — она не может стоить 10 секунд."""
    monkeypatch.setenv("PERSONA_MAIL_PROBE", "1")

    async def _never(*_a, **_k):
        await asyncio.sleep(30)

    monkeypatch.setattr(mt.asyncio, "open_connection", _never)
    monkeypatch.setattr(mt, "PROBE_TIMEOUT", 0.2)

    ok, reason = await mt.reachable(_cfg(mt.SMTP_STARTTLS))
    assert ok is False
    assert reason == "timeout"


# ── delivery_status() = реальная достижимость ───────────────────────────────


def _reachability(monkeypatch, *, ok: bool, reason: str = "ConnectionRefusedError") -> list[tuple[str, int]]:
    """Подменить сетевую пробу, записав, куда она ходила."""
    monkeypatch.setenv("PERSONA_MAIL_PROBE", "1")
    seen: list[tuple[str, int]] = []

    async def _fake(host: str, port: int, timeout: float) -> tuple[bool, str]:
        seen.append((host, port))
        return (True, "") if ok else (False, reason)

    monkeypatch.setattr(mt, "_tcp_reachable", _fake)
    mt.reset_probe_cache()
    return seen


async def _configure_smtp(db, **overrides: str) -> None:
    values = {
        "smtp_enabled": "true", "smtp_host": "smtp.example.test", "smtp_port": "587",
        "smtp_from": "persona@example.test", "smtp_to": "owner@example.test",
        "smtp_tls": "true",
    }
    for key, value in (values | overrides).items():
        await set_kv(db, key, value)


@pytest.mark.asyncio
async def test_a_configured_but_unreachable_transport_is_not_ok(db, monkeypatch) -> None:
    """ГЛАВНЫЙ тест: ровно состояние прода — конфиг полный, порт закрыт."""
    from app.smtp_delivery import delivery_status

    pytest.importorskip("aiosmtplib")
    await _configure_smtp(db)
    probed = _reachability(monkeypatch, ok=False)

    assert await delivery_status() == "unreachable"
    assert probed == [("smtp.example.test", 587)]


@pytest.mark.asyncio
async def test_a_reachable_transport_is_ok(db, monkeypatch) -> None:
    from app.smtp_delivery import delivery_status

    pytest.importorskip("aiosmtplib")
    await _configure_smtp(db)
    _reachability(monkeypatch, ok=True)

    assert await delivery_status() == "ok"


@pytest.mark.asyncio
async def test_status_follows_the_transport_not_the_smtp_row(db, monkeypatch) -> None:
    """Сменили транспорт — сменился и адресат пробы. Статус про ТО, что реально будет."""
    from app.smtp_delivery import delivery_status

    await _configure_smtp(db, mail_transport="http_api")
    monkeypatch.setenv(mt.API_KEY_ENV, FAKE_KEY)
    probed = _reachability(monkeypatch, ok=True)

    assert await delivery_status() == "ok"
    # SMTP-хост из настроек больше не при делах — идём в HTTPS провайдера.
    assert probed == [(mt.HTTP_API_HOST, 443)]


@pytest.mark.asyncio
async def test_http_api_without_a_key_says_so_and_does_not_probe(db, monkeypatch) -> None:
    """Нет ключа — честный ``no_credentials`` с инструкцией, а не ``ok``/``error``."""
    from app.smtp_delivery import delivery_status, send_email

    await _configure_smtp(db, mail_transport="http_api")
    probed = _reachability(monkeypatch, ok=True)

    assert await delivery_status() == "no_credentials"
    assert probed == []  # незачем стучаться, отправлять всё равно нечем

    result = await send_email("member@example.test", "Тема", "Тело")
    assert result["status"] == "no_credentials"
    assert mt.API_KEY_ENV in str(result["hint"])


@pytest.mark.asyncio
async def test_an_unreachable_transport_refuses_before_opening_a_socket(db, monkeypatch) -> None:
    """Недостижимый транспорт = НЕ ПЫТАЕМСЯ. Именно это и стоило 16 секунд."""
    from app.smtp_delivery import send_email

    pytest.importorskip("aiosmtplib")
    await _configure_smtp(db)
    _reachability(monkeypatch, ok=False)

    async def _must_not_run(*_a, **_k):
        raise AssertionError("отправка не должна начинаться при недостижимом транспорте")

    monkeypatch.setattr(mt, "_send_smtp", _must_not_run)
    monkeypatch.setattr(mt, "_send_http_api", _must_not_run)

    result = await send_email("member@example.test", "Тема", "Тело")
    assert result["status"] == "unreachable"


@pytest.mark.asyncio
async def test_the_negative_probe_is_cached(db, monkeypatch) -> None:
    """Закрытый порт отвечает отказом ~1.2 с — платить это на каждой странице нельзя."""
    from app.smtp_delivery import delivery_status

    pytest.importorskip("aiosmtplib")
    await _configure_smtp(db)
    probed = _reachability(monkeypatch, ok=False)

    assert await delivery_status() == "unreachable"
    assert await delivery_status() == "unreachable"
    assert len(probed) == 1


@pytest.mark.asyncio
async def test_status_still_answers_the_old_way_when_nothing_is_configured(db, monkeypatch) -> None:
    """Старые значения не изменили смысла: выключено — это выключено."""
    from app.smtp_delivery import delivery_status

    _reachability(monkeypatch, ok=True)
    await _configure_smtp(db, smtp_enabled="false")
    assert await delivery_status() == "disabled"

    await _configure_smtp(db, smtp_host="")
    assert await delivery_status() == "misconfigured"


# ── секреты не утекают ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_credential_reaches_a_log_record_or_a_status_dict(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Провайдер вернул наш ключ в тексте ошибки — наружу он всё равно не уйдёт.

    «Не обязан возвращать» — не то же самое, что «не может»: ключ, однажды
    попавший в лог, оттуда уже не достать.
    """

    class _Response:
        status_code = 422
        text = f"key {FAKE_KEY} rejected"

        @staticmethod
        def json() -> dict[str, str]:
            return {"message": f"The API key {FAKE_KEY} is not valid for {FAKE_PASS}"}

    class _Client:
        def __init__(self, **_kw) -> None: ...
        async def __aenter__(self): return self
        async def __aexit__(self, *_exc): return False
        async def post(self, *_a, **_k): return _Response()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    with caplog.at_level(logging.DEBUG):
        result = await mt.deliver(_cfg(mt.HTTP_API), _msg())

    assert result["status"] == "error"
    blob = repr(result) + "\n".join(
        r.getMessage() + repr(getattr(r, "__dict__", {})) for r in caplog.records
    )
    assert FAKE_KEY not in blob, "ключ провайдера утёк"
    assert FAKE_PASS not in blob, "пароль SMTP утёк"
    # …и при этом ошибка осталась ПОНЯТНОЙ, а не превратилась в «***».
    assert "not valid" in str(result["error"])


@pytest.mark.asyncio
async def test_a_raising_transport_does_not_leak_the_key_either(monkeypatch) -> None:
    async def _boom(*_a, **_k):
        raise RuntimeError(f"auth failed for Bearer {FAKE_KEY}")

    monkeypatch.setattr(mt, "_send_http_api", _boom)
    result = await mt.deliver(_cfg(mt.HTTP_API), _msg())
    assert FAKE_KEY not in repr(result)


@pytest.mark.asyncio
async def test_the_provider_key_is_never_read_from_the_database(db, monkeypatch) -> None:
    """Ключ живёт в env/файле. Строка в kv не должна его подсунуть."""
    await set_kv(db, "resend_api_key", "re_FROM_THE_DATABASE")
    await set_kv(db, "mail_transport", "http_api")
    assert mt.read_api_key() == ""

    from app.smtp_delivery import _build_config, _load_settings

    cfg = _build_config(await _load_settings())
    assert cfg.api_key == ""
    assert "re_FROM_THE_DATABASE" not in repr(cfg)


def test_the_key_can_come_from_the_data_dir_file(monkeypatch, tmp_path) -> None:
    """Второй разрешённый источник: файл в PERSONA_DATA_DIR, вне репозитория."""
    from app.settings import get_settings

    monkeypatch.setenv("PERSONA_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()  # type: ignore[attr-defined]
    (tmp_path / mt.API_KEY_FILENAME).write_text(f"  {FAKE_KEY}\n", encoding="utf-8")
    assert mt.read_api_key() == FAKE_KEY


# ── /auth/forgot: восстановление аккаунта и честная деградация ──────────────


@pytest_asyncio.fixture
async def auth_client(monkeypatch):
    """Клиент для auth-роутов + перехват писем на уровне транспорта."""
    delivered: list[dict[str, str]] = []
    state = {"status": "ok"}

    async def _fake_status() -> str:
        return state["status"]

    async def _fake_send(to_addr, subject, body_text, body_html=None):
        if state["status"] != "ok":
            return {"status": state["status"]}
        delivered.append({"to": to_addr, "subject": subject, "text": body_text})
        return {"status": "sent", "to": to_addr}

    monkeypatch.setattr("app.smtp_delivery.delivery_status", _fake_status)
    monkeypatch.setattr(auth_routes, "send_email", _fake_send)
    monkeypatch.setattr(auth_routes, "_rate_allow", lambda *a, **k: True)

    await init_database()
    app = FastAPI()
    app.include_router(auth_routes.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, delivered, state


@pytest.mark.asyncio
async def test_forgot_sends_a_recovery_link_when_mail_works(auth_client) -> None:
    client, delivered, _state = auth_client
    from app.auth.users import create_user

    await create_user("locked-out@example.test", "correct-horse-battery", None)

    response = await client.post("/auth/forgot", data={"email": "locked-out@example.test"})

    assert response.status_code == 200
    assert len(delivered) == 1
    assert delivered[0]["to"] == "locked-out@example.test"
    # Ссылка обязана вести на смену пароля, иначе «восстановление» ничего не
    # восстанавливает — человек просто оказывается залогинен со старым паролем,
    # которого он не знает.
    assert "/auth/set-password" in delivered[0]["text"]


@pytest.mark.asyncio
async def test_forgot_actually_unlocks_a_member_who_lost_the_password(auth_client) -> None:
    """Сквозной сценарий блокировки, ради которого всё и затевалось.

    Участник зарегистрировался, потерял показанный на экране пароль и сегодня
    не имеет НИ ОДНОГО способа вернуться. Проверяем весь путь целиком: письмо →
    magic-ссылка → форма пароля → вход новым паролем.
    """
    import re

    client, delivered, _state = auth_client
    from app.auth.users import authenticate, create_user

    await create_user("lost@example.test", "the-forgotten-one", None)

    await client.post("/auth/forgot", data={"email": "lost@example.test"})
    link = re.search(r"/auth/magic/([A-Za-z0-9_\-]+)", delivered[0]["text"])
    assert link, f"в письме нет magic-ссылки: {delivered[0]['text']!r}"

    hop = await client.get(
        f"/auth/magic/{link.group(1)}", params={"next": "/auth/set-password"},
        follow_redirects=False,
    )
    assert hop.status_code == 303
    assert hop.headers["location"] == "/auth/set-password"

    # Форма открывается участнику (а не только владельцу) — иначе ссылка ведёт в 403.
    assert (await client.get("/auth/set-password")).status_code == 200

    reset = await client.post("/auth/set-password", data={"password": "a-brand-new-secret-42"},
                              follow_redirects=False)
    assert reset.status_code == 303

    assert await authenticate("lost@example.test", "a-brand-new-secret-42") is not None
    assert await authenticate("lost@example.test", "the-forgotten-one") is None


@pytest.mark.asyncio
async def test_forgot_degrades_honestly_when_mail_is_down(auth_client) -> None:
    """Почта лежит — страница НЕ имеет права обещать письмо."""
    client, delivered, state = auth_client
    from app.auth.users import create_user

    await create_user("locked-out@example.test", "correct-horse-battery", None)
    state["status"] = "unreachable"

    response = await client.post(
        "/auth/forgot", data={"email": "locked-out@example.test"},
        headers={"Accept": "application/json"},
    )

    body = response.json()
    assert body["ok"] is True
    assert body["delivered"] is False
    assert "не уход" in body["message"]  # «почта … не уходит»
    assert delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mail_status", ["ok", "unreachable"])
async def test_forgot_never_reveals_whether_the_account_exists(auth_client, mail_status) -> None:
    """Честность про ИНСТАНС не должна превратиться в перечисление аккаунтов.

    Ответ для существующего и несуществующего адреса обязан совпадать байт в
    байт при любом состоянии почты.
    """
    client, _delivered, state = auth_client
    from app.auth.users import create_user

    await create_user("real@example.test", "correct-horse-battery", None)
    state["status"] = mail_status
    headers = {"Accept": "application/json"}

    known = await client.post("/auth/forgot", data={"email": "real@example.test"}, headers=headers)
    unknown = await client.post("/auth/forgot", data={"email": "ghost@example.test"}, headers=headers)

    assert known.status_code == unknown.status_code
    assert known.json() == unknown.json()
