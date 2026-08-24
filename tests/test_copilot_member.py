"""Копилот сайта для УЧАСТНИКА: гейт, граница данных, свои действия.

Что здесь закрепляется (менять только вместе с ``app/web/routes/copilot.py`` и
``app/llm/copilot_stream.py``):

* участник со СВОЕЙ моделью получает нормальный стрим, и клиент собирается
  именно под него (``make_client(..., user_id=<member>)``) — чужой ПК-воркер
  владельца не трогается;
* участник БЕЗ модели получает ровно один кадр ``llm_not_configured`` со
  ссылкой ``/settings/llm`` — не 500 и не «режим ИИ везде выключен» (эта
  подпись вела на owner-only страницу, которую он не может открыть);
* владелец — как раньше: его мастер-флаг «ИИ везде» решает всё;
* в промпт участника не попадает НИЧЕГО из данных владельца: захват экрана,
  OCR, аудио, часовые карточки, заметки, напоминания, его чаты, его личная
  память и его глобальный ``chat_system_prompt``;
* подсказки по настройкам ссылаются только на member-достижимые пути;
* действие «включи тёмную тему» пишет ЕГО ``user_settings``, а глобальные
  kv-строки инстанса остаются нетронутыми.

Ни один тест не ходит в сеть: клиент модели подставной.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner as owner_mod
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.llm import copilot_stream as copilot_stream_mod
from app.llm.client import LLMNotConfigured
from app.llm.copilot_stream import stream_copilot
from app.storage.db import get_connection, init_database
from app.storage.repository import get_kv, get_user_kv, set_kv, set_user_kv
from app.web import templates_engine
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware
from app.web.routes import copilot as copilot_route

# ── Канарейки: данные ВЛАДЕЛЬЦА, которых в промпте участника быть не должно ──
CANARY_HOURLY = "КАНАРЕЙКА-ЧАС-ВЛАДЕЛЬЦА-CP01"
CANARY_TRANSCRIPT = "КАНАРЕЙКА-РАСШИФРОВКА-CP02"
CANARY_NOTE = "КАНАРЕЙКА-ЗАМЕТКА-CP03"
CANARY_REMINDER = "КАНАРЕЙКА-НАПОМИНАНИЕ-CP04"
CANARY_CHAT = "КАНАРЕЙКА-ЧАТ-ВЛАДЕЛЬЦА-CP05"
CANARY_PROMPT = "КАНАРЕЙКА-ХАРАКТЕР-ВЛАДЕЛЬЦА-CP06"
CANARY_SCREENSHOT = "КАНАРЕЙКА-СКРИНШОТ-CP07"
CANARY_AUDIO = "КАНАРЕЙКА-МИКРОФОН-CP08"
CANARY_OWNER_FACT = "КАНАРЕЙКА-ЛИЧНАЯ-ПАМЯТЬ-ВЛАДЕЛЬЦА-CP09"

ALL_CANARIES = (
    CANARY_HOURLY,
    CANARY_TRANSCRIPT,
    CANARY_NOTE,
    CANARY_REMINDER,
    CANARY_CHAT,
    CANARY_PROMPT,
    CANARY_SCREENSHOT,
    CANARY_AUDIO,
    CANARY_OWNER_FACT,
)

#: Пути, которых участник достичь не может — в его подсказках им не место.
OWNER_ONLY_HREFS = ("/timeline", "/settings/capture", "/root", "/admin")


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


class FakeLLM:
    """Подставной клиент: помнит, от чьего имени его собрали и что ему дали."""

    def __init__(self, reply: str = "Готово.") -> None:
        self.calls: list[tuple[str, int | None]] = []
        self.requests: list[object] = []
        self.reply = reply

    def factory(self, *, kind: str = "unknown", user_id: int | None = None, **_kw):
        self.calls.append((kind, user_id))
        outer = self

        class _Client:
            provider = "fake"

            async def complete(self, request):
                outer.requests.append(request)
                return outer.reply

            async def stream(self, request):
                outer.requests.append(request)
                yield outer.reply

        return _Client()

    @property
    def last_prompt(self) -> str:
        request = self.requests[-1]
        return f"{request.system}\n{request.user}"  # type: ignore[attr-defined]


async def _seed_owner_private_data(owner_id: int) -> None:
    """Личные данные ВЛАДЕЛЬЦА — ровно те источники, которые надо не пустить."""
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO hourly_card "
            "(hour_start, hour_end, summary, screen_count, audio_seconds, "
            " transcript_excerpt, llm_enriched, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))",
            (
                "2026-08-20T10:00:00",
                "2026-08-20T10:59:59",
                CANARY_HOURLY,
                12,
                300,
                CANARY_TRANSCRIPT,
            ),
        )
        await conn.execute(
            "INSERT INTO screenshots "
            "(captured_at, monitor_index, width, height, phash, app_name, "
            " window_title) VALUES (?, 0, 1920, 1080, ?, 'Telegram', ?)",
            ("2026-08-20T10:15:00", CANARY_SCREENSHOT, CANARY_SCREENSHOT),
        )
        await conn.execute(
            "INSERT INTO audio_segment "
            "(captured_at, ended_at, duration_seconds, codec, path, size_bytes, "
            " transcript) VALUES (?, ?, 60.0, 'opus', ?, 1024, ?)",
            (
                "2026-08-20T10:20:00",
                "2026-08-20T10:21:00",
                "/tmp/canary-copilot.opus",
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
        cursor = await conn.execute(
            "INSERT INTO chat_session "
            "(user_id, title, created_at, updated_at, summary_up_to_id, "
            " auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (owner_id, CANARY_CHAT),
        )
        session_id = cursor.lastrowid
        await conn.execute(
            "INSERT INTO chat_message "
            "(session_id, role, content, created_at, is_streaming, is_pinned, "
            " access_count) VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (session_id, CANARY_CHAT),
        )
        # Личная память владельца — тоже per-user таблица; проверяем, что
        # копилот участника читает ЕГО строку, а не первую попавшуюся.
        await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (owner_id, CANARY_OWNER_FACT),
        )
        # Характер владельца живёт в ГЛОБАЛЬНОМ kv.
        await set_kv(conn, "chat_system_prompt", CANARY_PROMPT)
        await conn.commit()


@pytest_asyncio.fixture
async def env(db, monkeypatch: pytest.MonkeyPatch):
    """Владелец (с личными данными) + участник + подставная модель."""
    owner_user = await create_user("owner@copilot.test", "owner-pass-123")
    member_user = await create_user("member@copilot.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    # Глобальный провайдер владельца — его домашний ПК. Участнику он не положен.
    await set_kv(db, "llm_provider", "worker")
    _reset_caches()
    await _seed_owner_private_data(owner_user["id"])

    fake = FakeLLM()
    monkeypatch.setattr(copilot_stream_mod, "make_client", fake.factory)
    try:
        yield db, owner_user, member_user, fake
    finally:
        _reset_caches()


async def _configure_member_llm(user_id: int) -> None:
    """Дать участнику СВОЮ модель (свой провайдер + свой ключ)."""
    async with get_connection() as conn:
        await set_user_kv(conn, user_id, "llm_provider", "groq")
        await set_user_kv(conn, user_id, "byo_api_key_groq", "gsk_test_key_1234567890")
    templates_engine._user_kv_value_cache.clear()


def _app():
    from fastapi import FastAPI

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(copilot_route.router)
    return app


@pytest_asyncio.fixture
async def client(env):
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _as(ac: AsyncClient, uid: int) -> None:
    ac.cookies.clear()
    token, _ = await issue_session(uid)
    ac.cookies.set(SESSION_COOKIE_NAME, token)


def _frames(body: str) -> list[dict]:
    """SSE-текст → список event-ов (heartbeat-комментарии пропускаем)."""
    out: list[dict] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            out.append(json.loads(line.removeprefix("data: ")))
    return out


# ── 1. Гейт: у кого копилот работает ────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_with_own_model_gets_real_answer(env, client) -> None:
    """Участник со своей моделью получает поток, собранный ПОД НЕГО."""
    _db, _owner_user, member_user, fake = env
    await _configure_member_llm(member_user["id"])
    await _as(client, member_user["id"])

    response = await client.get(
        "/api/copilot/ask", params={"q": "что тут можно сделать?", "page_url": "/chat"}
    )
    assert response.status_code == 200
    frames = _frames(response.text)
    types = [frame["type"] for frame in frames]
    assert types[0] == "meta"
    assert "delta" in types and types[-1] == "done"
    assert frames[-1]["full_answer"] == "Готово."

    # Клиент собран именно под участника — чужой ПК-воркер не задействован.
    assert fake.calls, "модель не звали вовсе"
    kind, uid = fake.calls[-1]
    assert kind == "copilot"
    assert uid == member_user["id"]


@pytest.mark.asyncio
async def test_member_without_model_gets_llm_not_configured(env, client) -> None:
    """Нет своей модели → честная причина и ссылка, а не «ИИ везде выключен»."""
    _db, _owner_user, member_user, fake = env
    await _as(client, member_user["id"])

    response = await client.get("/api/copilot/ask", params={"q": "привет"})
    assert response.status_code == 200
    frames = _frames(response.text)
    assert len(frames) == 1, frames
    assert frames[0] == {
        "type": "error",
        "reason": "llm_not_configured",
        "message": "Свой AI не подключён — открой /settings/llm",
        "href": "/settings/llm",
    }
    # Ни намёка на владельческий тумблер и его страницу.
    assert "disabled" not in response.text
    assert "ai-everywhere" not in response.text
    # И модель не звали вовсе (чужую квоту не тратим).
    assert fake.calls == []


@pytest.mark.asyncio
async def test_member_with_borrowed_model_is_allowed(env, client) -> None:
    """Одолженная другом модель — тоже «есть модель» (app/llm/grants.py)."""
    _db, owner_user, member_user, _fake = env
    friend = await create_user("friend@copilot.test", "friend-pass-123")
    await _configure_member_llm(friend["id"])
    from app.llm import grants
    from app.social import repository as social

    request_id = await social.send_request(friend["id"], member_user["id"])
    assert await social.accept_request(request_id, member_user["id"])
    await grants.upsert_grant(friend["id"], member_user["id"], daily_limit=10)
    await _as(client, member_user["id"])

    response = await client.get("/api/copilot/ask", params={"q": "привет"})
    frames = _frames(response.text)
    assert frames[0]["type"] == "meta", frames
    assert owner_user["id"] != member_user["id"]


@pytest.mark.asyncio
async def test_owner_gate_is_ai_everywhere(env, client) -> None:
    """Владелец: мастер-флаг «ИИ везде» решает всё, как у прочих ИИ-поверхностей."""
    db, owner_user, _member_user, _fake = env
    await _as(client, owner_user["id"])

    await set_kv(db, "ai_everywhere", "0")
    off = await client.get("/api/copilot/ask", params={"q": "привет"})
    assert _frames(off.text) == [{"type": "error", "reason": "disabled"}]

    await set_kv(db, "ai_everywhere", "1")
    on = await client.get("/api/copilot/ask", params={"q": "привет"})
    types = [frame["type"] for frame in _frames(on.text)]
    assert types[0] == "meta" and types[-1] == "done"


@pytest.mark.asyncio
async def test_member_is_not_affected_by_owner_toggle(env, client) -> None:
    """Ядро бага: тумблер ЧУЖОГО аккаунта не выключает копилот участника."""
    db, _owner_user, member_user, _fake = env
    await set_kv(db, "ai_everywhere", "0")
    await _configure_member_llm(member_user["id"])
    await _as(client, member_user["id"])

    response = await client.get("/api/copilot/ask", params={"q": "привет"})
    frames = _frames(response.text)
    assert frames[0]["type"] == "meta", frames
    assert all(frame.get("reason") != "disabled" for frame in frames)


# ── 2. Граница данных ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_prompt_contains_no_owner_data(env) -> None:
    """Ни одна канарейка владельца не доезжает до модели участника."""
    _db, _owner_user, member_user, fake = env
    # Свой факт участник задал сам — он-то в промпте быть обязан.
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (member_user["id"], "Я пишу на Rust."),
        )
        await conn.commit()

    for mode in ("ask", "summary", "find_setting"):
        events = [
            event
            async for event in stream_copilot(
                "КАНАРЕЙКА расскажи что помнишь",
                page_url="/chat",
                mode=mode,
                user_id=int(member_user["id"]),
            )
        ]
        assert events[-1]["type"] == "done", events
        prompt = fake.last_prompt
        leaked = [canary for canary in ALL_CANARIES if canary in prompt]
        assert leaked == [], f"[{mode}] в промпт утекли данные владельца: {leaked}"

    assert "Я пишу на Rust." in fake.requests[0].user  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_member_prompt_has_no_owner_only_hrefs(env) -> None:
    """Подсказки участнику — только по достижимым для него страницам."""
    _db, _owner_user, member_user, fake = env

    events = [
        event
        async for event in stream_copilot(
            "где включить захват экрана и таймлайн",
            page_url="/settings/hub",
            mode="find_setting",
            user_id=int(member_user["id"]),
        )
    ]
    prompt = fake.last_prompt
    for href in OWNER_ONLY_HREFS:
        assert href not in prompt, f"в промпт участника попал owner-путь {href}"

    # …и в структурированном ответе тоже (его UI рисует кликабельной ссылкой).
    for setting in events[-1].get("settings", []):
        assert setting["href"] not in OWNER_ONLY_HREFS


@pytest.mark.asyncio
async def test_member_prompt_knows_the_page_and_member_catalogue(env) -> None:
    """Промпт участника называет страницу и перечисляет только его пути."""
    _db, _owner_user, member_user, fake = env

    events = [
        event
        async for event in stream_copilot(
            "что тут делать?",
            page_url="/settings/llm?tab=key",
            mode="ask",
            user_id=int(member_user["id"]),
        )
    ]
    assert events[-1]["type"] == "done"
    prompt = fake.last_prompt
    assert "/settings/llm" in prompt
    assert "Провайдер и ключ твоей модели" in prompt  # подпись из member-каталога
    assert "/chat" in prompt  # рабочие экраны участника
    for href in OWNER_ONLY_HREFS:
        assert href not in prompt


@pytest.mark.asyncio
async def test_owner_prompt_stays_the_plain_system_line(env) -> None:
    """Владельцу системный промпт копилота не поменяли."""
    _db, owner_user, _member_user, fake = env

    events = [
        event
        async for event in stream_copilot(
            "как дела", page_url="/now", mode="ask", user_id=int(owner_user["id"])
        )
    ]
    assert events[-1]["type"] == "done"
    assert fake.requests[-1].system == copilot_stream_mod._COPILOT_SYSTEM  # type: ignore[attr-defined]


# ── 3. Действия участника пишутся в ЕГО настройки ───────────────────────────


@pytest.mark.asyncio
async def test_member_theme_action_writes_own_user_settings(env) -> None:
    """«Включи тёмную тему» → user_settings участника, глобальный kv не тронут."""
    db, _owner_user, member_user, fake = env
    # Тема ВЛАДЕЛЬЦА — отдельная ГЛОБАЛЬНАЯ строка; ставим заметное значение,
    # чтобы «не тронули» проверялось по факту, а не по совпадению с дефолтом.
    await set_kv(db, "theme", "light")

    events = [
        event
        async for event in stream_copilot(
            "включи тёмную тему",
            page_url="/settings/theme",
            user_id=int(member_user["id"]),
        )
    ]
    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    assert events[-1]["href"] == "/settings/theme"
    assert await get_user_kv(db, member_user["id"], "theme") == "dark"
    assert await get_kv(db, "theme") == "light"
    # И модель для этого не звали — действие применяется без запроса.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_member_tools_action_never_touches_global_flags(env) -> None:
    """«Включи инструменты» — свои ``user_settings``, не глобальные kv инстанса."""
    db, _owner_user, member_user, _fake = env

    events = [
        event
        async for event in stream_copilot(
            "включи инструменты", page_url="/settings/advanced",
            user_id=int(member_user["id"]),
        )
    ]
    assert [event["type"] for event in events] == ["meta", "delta", "done"]
    assert events[-1]["href"] == "/settings/advanced"
    assert await get_user_kv(db, member_user["id"], "feat_tools") == "1"
    assert await get_user_kv(db, member_user["id"], "advanced_mode") == "1"
    # Флаги ВЛАДЕЛЬЦА нетронуты — это и был исходный баг.
    assert await get_kv(db, "feat_tools") is None
    assert await get_kv(db, "advanced_mode") is None


@pytest.mark.asyncio
async def test_member_cannot_flip_owner_master_switch(env) -> None:
    """«Включи ИИ везде» участнику недоступно: это глобальный флаг владельца."""
    db, _owner_user, member_user, _fake = env
    await set_kv(db, "ai_everywhere", "0")

    events = [
        event
        async for event in stream_copilot(
            "включи ии везде", page_url="/chat", user_id=int(member_user["id"])
        )
    ]
    # Флаг инстанса не переключён — ни в kv, ни «себе в user_settings».
    assert await get_kv(db, "ai_everywhere") == "0"
    assert await get_user_kv(db, member_user["id"], "ai_everywhere") is None
    # Действие не применено → обычный путь ответа моделью, без ссылки-действия.
    assert events[-1].get("href") != "/settings/ai-everywhere"


# ── 4. Мягкая деградация ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_stream_never_500s_when_provider_dies(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Провайдер участника отвалился → кадр с причиной, а не исключение."""
    _db, _owner_user, member_user, _fake = env

    def _boom(**_kw):
        raise LLMNotConfigured("no key")

    monkeypatch.setattr(copilot_stream_mod, "make_client", _boom)
    events = [
        event
        async for event in stream_copilot(
            "привет", page_url="/chat", user_id=int(member_user["id"])
        )
    ]
    assert [event["type"] for event in events] == ["meta", "error"]
    assert events[-1]["reason"] == "llm_not_configured"
    assert events[-1]["href"] == "/settings/llm"


# ── 5. Палитра Cmd+K роле-осведомлена ───────────────────────────────────────


@pytest_asyncio.fixture
async def palette_client(env):
    from fastapi import FastAPI

    from app.web.routes import palette as palette_routes

    await init_database()
    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(palette_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_palette_for_member_has_no_owner_routes(env, palette_client) -> None:
    """Участник видит свои экраны; owner-маршрутов и чужих тегов там нет."""
    _db, owner_user, member_user, _fake = env
    async with get_connection() as conn:
        await conn.execute("INSERT INTO tags (name) VALUES (?)", (CANARY_NOTE,))
        await conn.commit()

    await _as(palette_client, member_user["id"])
    response = await palette_client.get("/api/palette.json")
    assert response.status_code == 200
    urls = [item["url"] for item in response.json()["items"]]
    assert "/chat" in urls
    assert "/settings/llm" in urls
    for href in ("/timeline", "/vault", "/audit", "/whitelist", "/settings/capture"):
        assert href not in urls, f"участнику показали owner-маршрут {href}"
    assert CANARY_NOTE not in response.text

    # Владелец получает прежний богатый список.
    await _as(palette_client, owner_user["id"])
    owner_urls = [
        item["url"] for item in (await palette_client.get("/api/palette.json")).json()["items"]
    ]
    assert "/vault" in owner_urls and "/whitelist" in owner_urls
