"""Сквозной аудит бесплатного MVP: реальный участник против НАСТОЯЩЕГО приложения.

В отличие от ``test_member_surface`` / ``test_member_settings_isolation`` (там
роуты-заглушки и проверяется решение ГЕЙТА), здесь поднимается полное
приложение ``create_app()`` и по нему ходит настоящий зарегистрированный
не-владелец. Это ловит то, что заглушки поймать не могут:

* 500 в реальных обработчиках member-поверхности (шаблон падает на member-
  контексте, SQL без user-фильтра, отсутствующая kv-строка и т.п.);
* утечку личных данных владельца (скриншоты/часовые карточки/заметки/
  напоминания/его чат) в тело ЛЮБОГО member-ответа;
* дырку в гейте на owner-only путях (должно быть 403 для /api/* и 303 → /chat
  для страниц, и НИ ОДНОГО 200);
* изоляцию LLM: у владельца в глобальном kv стоит ``worker`` (домашний ПК) —
  участник не должен ни поставить задачу в ``llm_job``, ни собрать клиента на
  чужом провайдере; со СВОИМ ключом — собирает своего.

kv ``owner_exclusive_mode`` здесь ВЫКЛ: это kill-switch, который сейчас держит
прод закрытым, а тесты проверяют состояние ПОСЛЕ его снятия.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv, set_user_kv
from app.web import templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import _is_member_path
from app.web.routes import setup_gate

# ── Метки-канарейки: личные данные ВЛАДЕЛЬЦА ────────────────────────────────
# Каждая строка уникальна, чтобы поиск по телу ответа не давал ложных срабатываний.
CANARY_HOURLY = "КАНАРЕЙКА-ЧАС-ВЛАДЕЛЬЦА-QX41"
CANARY_TRANSCRIPT = "КАНАРЕЙКА-РАСШИФРОВКА-QX42"
CANARY_NOTE = "КАНАРЕЙКА-ЗАМЕТКА-QX43"
CANARY_REMINDER = "КАНАРЕЙКА-НАПОМИНАНИЕ-QX44"
CANARY_CHAT = "КАНАРЕЙКА-ЧАТ-ВЛАДЕЛЬЦА-QX45"
CANARY_PROMPT = "КАНАРЕЙКА-ХАРАКТЕР-ВЛАДЕЛЬЦА-QX46"
CANARY_SCREENSHOT = "КАНАРЕЙКА-СКРИНШОТ-QX47"
CANARY_AUDIO = "КАНАРЕЙКА-МИКРОФОН-QX48"

ALL_CANARIES = (
    CANARY_HOURLY,
    CANARY_TRANSCRIPT,
    CANARY_NOTE,
    CANARY_REMINDER,
    CANARY_CHAT,
    CANARY_PROMPT,
    CANARY_SCREENSHOT,
    CANARY_AUDIO,
)

#: Owner-only выборка (~25 путей). Ни один не должен отдать 200 участнику.
OWNER_ONLY_PATHS: tuple[str, ...] = (
    "/timeline",
    "/search",
    "/now",
    "/notes",
    "/reminders",
    "/dashboard",
    "/analytics",
    "/stats",
    "/root",
    "/admin/mcp",
    "/settings/capture",
    "/devices",
    "/briefing",
    "/thoughts",
    "/memory",
    "/day/2026-08-01",
    "/api/dashboard/insights.json",
    "/api/timeline/hour/2026-08-01T10/summary",
    "/api/ai-calendar/parse",
    "/api/search/suggest.json",
    "/settings/thinking",
    "/settings/telegram-people",
    "/settings/ai-everywhere",
    "/api/settings/ai-search",
    "/settings/billing-admin",
)


def _reset_caches() -> None:
    """Сбросить ВСЕ процесс-глобальные TTL-кэши личности/настроек.

    Без этого решение гейта или тема/язык протекают между тестами (в каждом
    тесте своя временная БД, а кэш — модульный).
    """
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
    # ContextVar темы переживает границу теста — без сброса чужое значение
    # подменяет тему владельца (см. тот же сброс в test_member_settings_isolation).
    templates_engine.invalidate_theme_cache()
    i18n.invalidate_language_cache()


async def _seed_owner_private_data(owner_id: int) -> None:
    """Положить в БД личные данные владельца, помеченные канарейками."""
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
            "INSERT INTO notes (body, created_at, updated_at) "
            "VALUES (?, datetime('now'), datetime('now'))",
            (CANARY_NOTE,),
        )
        await conn.execute(
            "INSERT INTO reminders (body, due_date) VALUES (?, ?)",
            (CANARY_REMINDER, "2026-08-25T09:00:00"),
        )
        await conn.execute(
            "INSERT INTO screenshots "
            "(captured_at, monitor_index, width, height, phash, app_name, "
            " window_title) VALUES (?, 0, 1920, 1080, ?, ?, ?)",
            (
                "2026-08-20T10:15:00",
                CANARY_SCREENSHOT,
                "Telegram",
                CANARY_SCREENSHOT,
            ),
        )
        await conn.execute(
            "INSERT INTO audio_segment "
            "(captured_at, ended_at, duration_seconds, codec, path, size_bytes, "
            " transcript) VALUES (?, ?, 60.0, 'opus', ?, 1024, ?)",
            (
                "2026-08-20T10:20:00",
                "2026-08-20T10:21:00",
                "/tmp/canary.opus",
                CANARY_AUDIO,
            ),
        )
        # Чат ВЛАДЕЛЬЦА: сессия + сообщение с канарейкой.
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
        await conn.commit()


@pytest_asyncio.fixture
async def env():
    """Реальное приложение + владелец + участник, kill-switch ВЫКЛ."""
    await init_database()
    owner_user = await create_user("owner@smoke.test", "Zq7-frost-lantern-91")
    member_user = await create_user("member@smoke.test", "Kp4-velvet-harbour-38")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
        # Глобальные настройки ВЛАДЕЛЬЦА: провайдер — его домашний ПК,
        # характер — с канарейкой. Ни то, ни другое участнику не положено.
        await set_kv(conn, "llm_provider", "worker")
        await set_kv(conn, "chat_system_prompt", CANARY_PROMPT)
        await conn.commit()
    await _seed_owner_private_data(owner_user["id"])
    setup_gate._cache.mark_done()
    _reset_caches()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, owner_user, member_user
    finally:
        _reset_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


def _member_get_paths() -> list[str]:
    """Все GET-роуты member-поверхности БЕЗ параметров пути.

    Список берём из САМОГО приложения, а не хардкодим: если кто-то добавит
    роут под member-префикс, он автоматически попадёт под аудит.
    """
    app = create_app()
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "{" in path or "GET" not in methods:
            continue
        if _is_member_path(path):
            paths.add(path)
    return sorted(paths)


#: Роуты, которые ЛЕЖАТ под member-префиксом, но охраняются отдельной
#: owner-зависимостью в самом обработчике (защита в глубину — так и надо).
#: Участнику они отдают 403; страница при этом рисуется, просто без виджета.
#: Держим их списком, чтобы обход поверхности не считал 403 регрессией, но
#: и чтобы новый owner-guard под member-префиксом не проехал незамеченным.
OWNER_GUARDED_INSIDE_MEMBER_ZONE: frozenset[str] = frozenset(
    {
        # Дообучение памяти / векторный индекс — инстанс-глобальный движок.
        "/settings/memory/train/status",
        # История версий динамического системного промпта — владельца.
        "/settings/system-prompt/history",
    }
)


def _looks_like_leak(body: str) -> list[str]:
    """Какие канарейки видны в теле (сырьём или в \\uXXXX-экранировании)."""
    found = []
    for canary in ALL_CANARIES:
        escaped = json.dumps(canary, ensure_ascii=True)[1:-1]
        if canary in body or escaped in body:
            found.append(canary)
    return found


# ── A1. Участник проходит всю свою поверхность без 403/500 ──────────────────


@pytest.mark.asyncio
async def test_member_walks_whole_free_surface(env: Any) -> None:
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    failures: list[str] = []
    for path in _member_get_paths():
        response = await client.get(path, follow_redirects=False)
        status = response.status_code
        if status >= 500:
            failures.append(f"{path} → {status} (500!) {response.text[:200]}")
            continue
        if status == 403:
            if path not in OWNER_GUARDED_INSIDE_MEMBER_ZONE:
                failures.append(f"{path} → 403 (участнику должно быть можно)")
            continue
        if status in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            # Редирект «наружу» member-зоны = гейт выкинул участника.
            if location in ("/chat", "/landing", "/pending"):
                failures.append(f"{path} → {status} {location} (выкинут из зоны)")
    assert not failures, "member-поверхность недоступна:\n" + "\n".join(failures)


@pytest.mark.asyncio
async def test_owner_guarded_routes_inside_member_zone_stay_403(env: Any) -> None:
    """Фиксируем ровно то, что охраняется отдельно ВНУТРИ member-зоны.

    Если такой роут вдруг откроется участнику — это утечка глобального
    движка/истории владельца, и тест обязан покраснеть.
    """
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    for path in sorted(OWNER_GUARDED_INSIDE_MEMBER_ZONE):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code == 403, (
            f"{path} → {response.status_code}: owner-guard внутри member-зоны исчез"
        )


@pytest.mark.asyncio
async def test_member_key_pages_render_200(env: Any) -> None:
    """Ключевые страницы участника отдают именно 200 (шаблон отрисовался)."""
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    for path in (
        "/chat",
        "/onboarding",
        "/voice",
        "/graph",
        "/settings/hub",
        "/settings/llm",
        "/settings/memory",
        "/settings/profile",
        "/settings/system-prompt",
        "/settings/theme",
        "/settings/advanced",
        "/settings/skills",
        "/api/graph.json",
        "/api/llm/models",
        "/api/skills",
        "/api/account.json",
        "/api/settings/search",
    ):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code == 200, (
            f"{path} → {response.status_code}: {response.text[:300]}"
        )


# ── A2. Owner-only поверхность закрыта наглухо ──────────────────────────────


@pytest.mark.asyncio
async def test_owner_only_paths_never_return_200_to_member(env: Any) -> None:
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    leaks: list[str] = []
    for path in OWNER_ONLY_PATHS:
        response = await client.get(path, follow_redirects=False)
        status = response.status_code
        if status == 200:
            leaks.append(f"{path} → 200 УТЕЧКА: {response.text[:200]}")
            continue
        if path.startswith("/api/"):
            # JSON-эндпоинт: 403 от гейта (404/405 — роут иначе устроен, тоже
            # не утечка, но 200 быть не должно ни при каких раскладах).
            if status not in (403, 404, 405):
                leaks.append(f"{path} → {status} (ожидали 403)")
        else:
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if location != "/chat":
                    leaks.append(f"{path} → {status} {location} (ожидали /chat)")
            elif status not in (403, 404):
                leaks.append(f"{path} → {status} (ожидали 303 /chat)")
    assert not leaks, "owner-only поверхность протекает:\n" + "\n".join(leaks)


# ── A3. Ни одна канарейка владельца не видна участнику ──────────────────────


@pytest.mark.asyncio
async def test_member_never_sees_owner_private_data(env: Any) -> None:
    """Обходим ВСЮ member-поверхность и ищем в телах данные владельца."""
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    leaks: list[str] = []
    for path in _member_get_paths():
        response = await client.get(path, follow_redirects=False)
        found = _looks_like_leak(response.text)
        if found:
            leaks.append(f"{path} → {found}")
    # Плюс поиск/память чата: самые вероятные места утечки чужих сообщений.
    for path in (
        "/api/chat/sessions",
        "/api/chat/search?q=КАНАРЕЙКА",
        "/api/chat/memory?q=КАНАРЕЙКА",
        "/api/settings/search?q=КАНАРЕЙКА",
        "/api/copilot/ask?q=КАНАРЕЙКА",
    ):
        response = await client.get(path, follow_redirects=False)
        found = _looks_like_leak(response.text)
        if found:
            leaks.append(f"{path} → {found}")
    assert not leaks, "личные данные владельца видны участнику:\n" + "\n".join(leaks)


@pytest.mark.asyncio
async def test_member_chat_session_list_is_own_only(env: Any) -> None:
    """Список сессий участника не содержит сессию владельца."""
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    response = await client.get("/api/chat/sessions")
    assert response.status_code == 200
    body = response.text
    assert CANARY_CHAT not in body
    assert json.dumps(CANARY_CHAT, ensure_ascii=True)[1:-1] not in body


# ── A4. Изоляция LLM ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_send_does_not_enqueue_owner_worker_job(env: Any) -> None:
    """Глобальный провайдер = ``worker`` (ПК владельца) → участник его НЕ трогает.

    Отправка сообщения участником не должна класть НИ ОДНОЙ строки в
    ``llm_job``: это очередь домашнего ПК владельца. Ответ — мягкая деградация
    («свой AI не подключён»), а не расход чужого железа.
    """
    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    created = await client.post("/api/chat/sessions", json={"title": "член"})
    assert created.status_code in (200, 201), created.text
    session_id = created.json()["id"]

    response = await client.post(
        f"/api/chat/sessions/{session_id}/send",
        json={"question": "привет, кто ты?"},
    )
    # Не настроен свой LLM → что угодно кроме успешной генерации на чужом
    # провайдере. 500 тоже недопустим (это был бы необработанный путь).
    assert response.status_code != 500, response.text

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM llm_job")
        (jobs,) = await cursor.fetchone()
    assert jobs == 0, (
        f"участник поставил {jobs} задач(и) в очередь ПК владельца — "
        "провайдер worker обязан быть запрещён не-владельцу"
    )


@pytest.mark.asyncio
async def test_make_client_forbids_worker_for_member(env: Any) -> None:
    """``make_client(user_id=member)`` не собирает клиента на чужом конфиге."""
    from app.llm.client import LLMProviderForbidden, make_client

    _client, _owner_user, member_user = env

    # 1. Ничего своего не настроено → «не настроено», а НЕ глобальный worker.
    with pytest.raises(Exception) as excinfo:
        make_client(kind="chat", user_id=member_user["id"])
    assert "not configured" in str(excinfo.value).lower() or "не подключён" in str(
        excinfo.value
    ), str(excinfo.value)

    # 2. Участник ЯВНО выбрал worker → отдельный запрет.
    async with get_connection() as conn:
        await set_user_kv(conn, member_user["id"], "llm_provider", "worker")
        await conn.commit()
    with pytest.raises(LLMProviderForbidden):
        make_client(kind="chat", user_id=member_user["id"])


@pytest.mark.asyncio
async def test_make_client_builds_members_own_client(env: Any) -> None:
    """Со СВОИМ провайдером+ключом участник получает ИМЕННО своего клиента."""
    from app.llm.client import make_client

    _client, _owner_user, member_user = env

    async with get_connection() as conn:
        await set_user_kv(conn, member_user["id"], "llm_provider", "openai")
        await set_user_kv(
            conn, member_user["id"], "byo_api_key_openai", "sk-member-own-key"
        )
        await conn.commit()

    built = make_client(kind="chat", user_id=member_user["id"])
    assert built.provider == "openai"
    # Ключ — ЕГО, а не владельца (сети не касаемся, читаем поле клиента).
    inner = built._inner  # noqa: SLF001 — тест смотрит внутрь обёртки учёта
    assert inner._api_key == "sk-member-own-key"  # noqa: SLF001


@pytest.mark.asyncio
async def test_member_ollama_never_falls_back_to_local_server(env: Any) -> None:
    """Ollama без СВОЕГО URL → ошибка, а не localhost сервера Persona."""
    from app.llm.client import LLMNotConfigured, make_client

    _client, _owner_user, member_user = env

    async with get_connection() as conn:
        await set_user_kv(conn, member_user["id"], "llm_provider", "ollama")
        await conn.commit()

    with pytest.raises(LLMNotConfigured) as excinfo:
        make_client(kind="chat", user_id=member_user["id"])
    assert "localhost" not in str(excinfo.value).lower() or "твоего" in str(
        excinfo.value
    ).lower()

    # А со своим URL — собирается на НЕГО.
    async with get_connection() as conn:
        await set_user_kv(
            conn, member_user["id"], "byo_api_key_ollama", "http://192.168.1.10:11434"
        )
        await conn.commit()
    built = make_client(kind="chat", user_id=member_user["id"])
    assert built.provider == "ollama"
    assert "192.168.1.10" in built._inner._endpoint  # noqa: SLF001


# ── A4b. Системный промпт участника не содержит захват владельца ────────────


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
async def test_member_prompt_never_carries_owner_capture(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ключевой тест утечки: вопрос «что я делал сегодня» от УЧАСТНИКА.

    ``build_memory_context`` читает ``hourly_card`` / ``screenshots`` /
    ``audio_segment`` — глобальные таблицы ЗАХВАТА ВЛАДЕЛЬЦА (экран и микрофон,
    в них нет user_id). Если этот блок подмешивается в промпт не-владельцу, то
    личные данные владельца уезжают в ЧУЖОЙ провайдер по ЧУЖОМУ ключу.
    """
    from app.web.routes import chat_sessions as chat_routes

    client, _owner_user, member_user = env
    await _as(client, member_user["id"])

    # У участника свой провайдер — иначе поток оборвётся до сборки промпта.
    async with get_connection() as conn:
        await set_user_kv(conn, member_user["id"], "llm_provider", "openai")
        await set_user_kv(
            conn, member_user["id"], "byo_api_key_openai", "sk-member-own-key"
        )
        await conn.commit()

    prompts: list[str] = []
    monkeypatch.setattr(
        chat_routes,
        "make_client",
        lambda *a, **kw: _RecordingClient(prompts),
    )

    created = await client.post("/api/chat/sessions", json={"title": "член"})
    session_id = created.json()["id"]

    response = await client.post(
        f"/api/chat/sessions/{session_id}/send-stream",
        # «делал сегодня» — активити-интент: memory_context отдаёт свежие
        # карточки/приложения/речь БЕЗ фильтра по словам запроса.
        json={"question": "что я делал сегодня на компьютере?"},
    )
    assert response.status_code == 200, response.text

    assert prompts, "промпт не собрался — тест ничего не проверил"
    leaked = _looks_like_leak("\n".join(prompts))
    assert not leaked, (
        "В системный промпт УЧАСТНИКА попал захват экрана/микрофона "
        f"ВЛАДЕЛЬЦА: {leaked}"
    )


@pytest.mark.asyncio
async def test_owner_prompt_still_carries_own_capture(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контроль к тесту выше: у ВЛАДЕЛЬЦА блок активности на месте.

    Без этого теста «утечка закрыта» можно было бы получить, просто сломав
    фичу для всех.
    """
    from app.web.routes import chat_sessions as chat_routes

    client, owner_user, _member_user = env
    await _as(client, owner_user["id"])

    prompts: list[str] = []
    monkeypatch.setattr(
        chat_routes,
        "make_client",
        lambda *a, **kw: _RecordingClient(prompts),
    )

    created = await client.post("/api/chat/sessions", json={"title": "владелец"})
    session_id = created.json()["id"]
    response = await client.post(
        f"/api/chat/sessions/{session_id}/send-stream",
        json={"question": "что я делал сегодня на компьютере?"},
    )
    assert response.status_code == 200, response.text

    assert prompts, "промпт владельца не собрался"
    blob = "\n".join(prompts)
    assert CANARY_HOURLY in blob, (
        "владелец перестал видеть свой же захват — фича сломана, а не защищена"
    )


# ── A5. Владелец ничего не потерял ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_still_sees_own_private_data(env: Any) -> None:
    """Контроль: канарейки ДОСТУПНЫ владельцу — тесты выше не «зелёные пустышки»."""
    client, owner_user, _member_user = env
    await _as(client, owner_user["id"])

    graph = await client.get("/api/graph.json")
    assert graph.status_code == 200
    hourly = [n for n in graph.json()["nodes"] if str(n["id"]).startswith("h")]
    assert hourly, "у владельца часовые карточки пропали из графа — регрессия"
