"""Связность сайта глазами СВЕЖЕЗАРЕГИСТРИРОВАННОГО участника.

Соседние наборы стерегут ДОСТУП (`test_member_surface` — решение гейта,
`test_mvp_smoke_audit` — отсутствие 500 и утечек владельца). Здесь сторожим
ощущение «это один законченный продукт», а не набор экранов, собранных
разными руками:

* каждая member-страница отдаёт 200 и не роняет в разметку сырой Jinja;
* ни один отрендеренный ``href`` не уводит участника за пределы его зоны —
  allowlist строится ИЗ ``_MEMBER_PREFIXES`` (импортируем, а не копируем:
  копия неизбежно разъедется с гейтом);
* у пустого аккаунта на каждом ключевом экране есть пустое состояние с
  понятным следующим шагом (``data-empty-state``), а не пустая панель;
* каждый ключ ``t('…')``, который используют шаблоны, есть во ВСЕХ трёх
  языках, и наборы ключей ru/en/de совпадают ключ-в-ключ.

kv ``owner_exclusive_mode`` тут ВЫКЛ — тесты описывают состояние ПОСЛЕ
снятия kill-switch, то есть открытую регистрацию.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web import templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import (
    _MEMBER_PREFIXES,
    _PUBLIC_PREFIXES,
    _is_member_path,
    _is_public_path,
)
from app.web.routes import setup_gate

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "app" / "web" / "templates"
TRANSLATIONS_DIR = REPO_ROOT / "app" / "translations"
LANGS = ("ru", "en", "de")

#: ``{{ t('key') }}`` / ``{{ t("key") }}`` в шаблоне. Отрицательный lookbehind
#: отсекает хвосты чужих идентификаторов (``querySelectorAll('div')`` во
#: встроенном JS оканчивается на ``t(`` и без него попадал в выборку).
_T_CALL = re.compile(r"""(?<![A-Za-z0-9_.$])t\(\s*['"]([A-Za-z0-9_.]+)['"]""")

#: Динамически собираемые ключи вида ``t('dm_ai_mode_' ~ pref.mode)`` — префикс
#: реален, целого ключа в шаблоне нет. Сверяем такие по префиксу.
_DYNAMIC_KEY_PREFIXES = ("dm_ai_mode_",)

#: Экраны, которые обязаны иметь пустое состояние у аккаунта без данных.
#: Значение — маркер ``data-empty-state`` в разметке.
EMPTY_STATE_PAGES: dict[str, str] = {
    "/chat": "chat",
    "/messages": "messages",
    "/friends": "friends",
    "/graph": "graph",
    "/settings/memory": "memory",
    "/settings/skills": "skills",
    "/settings/llm/sharing": "llm-sharing",
}

#: Роуты ПОД member-префиксом, которые намеренно охраняются отдельной
#: owner-зависимостью в самом обработчике (см. тот же список в
#: test_mvp_smoke_audit). Страницами не являются — обход их пропускает.
OWNER_GUARDED_INSIDE_MEMBER_ZONE: frozenset[str] = frozenset(
    {
        "/settings/memory/train/status",
        "/settings/system-prompt/history",
    }
)

#: Ссылки, которые ведут наружу приложения или обрабатываются не HTTP-GET.
#: Их обход не проверяет (POST-формы, скачивания, mailto/https).
_SKIP_HREF_PREFIXES = ("/static/", "/api/")


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


@pytest_asyncio.fixture
async def fresh_member():
    """Настоящее приложение + владелец + УЧАСТНИК С ПУСТЫМ АККАУНТОМ.

    Никаких сидов: ни чатов, ни друзей, ни сообщений, ни фактов памяти —
    ровно то, что видит человек через минуту после регистрации.
    """
    await init_database()
    owner_user = await create_user("keeper@coherence.test", "Zq7-frost-lantern-91")
    member_user = await create_user("newbie@coherence.test", "Kp4-velvet-harbour-38")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
        await set_kv(conn, "ui_language", "ru")
        await conn.commit()
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


def _member_pages() -> list[str]:
    """HTML-страницы member-зоны без параметров пути — из самого приложения.

    Список НЕ хардкодим: новый роут под member-префиксом попадает под обход
    автоматически, вместе с требованием отдать 200 и не течь ссылками наружу.
    """
    app = create_app()
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if not path or "{" in path or "GET" not in methods:
            continue
        if path.startswith("/api/") or path in OWNER_GUARDED_INSIDE_MEMBER_ZONE:
            continue
        if _is_member_path(path):
            paths.add(path)
    return sorted(paths)


def _hrefs(html: str) -> set[str]:
    """Внутренние ``href="/…"`` из разметки (без якорей и query)."""
    out: set[str] = set()
    for raw in re.findall(r'href="(/[^"]*)"', html):
        path = raw.split("#", 1)[0].split("?", 1)[0]
        if path:
            out.add(path)
    return out


def _member_reachable(path: str) -> bool:
    """Достижим ли путь участнику: его зона, публичная зона или ``/auth/*``."""
    return _is_member_path(path) or _is_public_path(path) or path.startswith("/auth/")


# ── i18n ────────────────────────────────────────────────────────────────────


def _load(lang: str) -> dict[str, str]:
    return json.loads((TRANSLATIONS_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def test_translation_files_have_identical_key_sets() -> None:
    """ru/en/de — ключ-в-ключ.

    Расхождение означает, что часть интерфейса на одном языке молча падает на
    фолбэк другого: для пользователя это лоскутный экран из двух языков.
    """
    tables = {lang: set(_load(lang)) for lang in LANGS}
    union: set[str] = set().union(*tables.values())
    missing = {lang: sorted(union - keys) for lang, keys in tables.items()}
    assert not any(missing.values()), f"расходятся наборы ключей: {missing}"


def test_every_template_translation_key_exists_in_all_languages() -> None:
    """Каждый ``t('…')`` из шаблонов есть во всех трёх таблицах.

    ``t()`` на отсутствующем ключе возвращает САМ КЛЮЧ, поэтому промах не
    падает, а тихо печатает ``friends_title`` вместо заголовка — QA такое
    ловит через раз. Ловим статически.
    """
    tables = {lang: _load(lang) for lang in LANGS}
    missing: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        for key in _T_CALL.findall(path.read_text(encoding="utf-8")):
            if any(key.startswith(p) for p in _DYNAMIC_KEY_PREFIXES):
                continue
            absent = [lang for lang in LANGS if key not in tables[lang]]
            if absent:
                missing.setdefault(f"{key} ({path.name})", []).extend(absent)
    assert not missing, f"ключи есть в шаблонах, но не в переводах: {missing}"


def test_dynamic_translation_key_prefixes_are_fully_covered() -> None:
    """У склеиваемых ключей (``'dm_ai_mode_' ~ mode``) есть все варианты."""
    tables = {lang: _load(lang) for lang in LANGS}
    for prefix in _DYNAMIC_KEY_PREFIXES:
        variants = {k for k in tables["ru"] if k.startswith(prefix)}
        assert variants, f"нет ни одного ключа с префиксом {prefix}"
        for lang in LANGS:
            absent = sorted(variants - set(tables[lang]))
            assert not absent, f"{lang}: не хватает {absent}"


# ── Обход поверхности участника ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_member_page_renders_for_an_empty_account(
    fresh_member: Any,
) -> None:
    """Свежий аккаунт без данных: каждая его страница отдаёт 200."""
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    failures: list[str] = []
    for path in _member_pages():
        r = await client.get(path, follow_redirects=False)
        location = r.headers.get("location", "")
        # 308 — это alias со слэшем на конце (``/settings/llm/sharing/``),
        # который Starlette сам схлопывает. Считаем нормой, но только если
        # цель осталась внутри member-зоны.
        if r.status_code == 308 and _member_reachable(location):
            continue
        if r.status_code != 200:
            failures.append(f"{path} → {r.status_code} {location}")
    assert not failures, "у пустого аккаунта страницы не рисуются:\n" + "\n".join(
        failures
    )


@pytest.mark.asyncio
async def test_member_pages_do_not_leak_raw_template_syntax(
    fresh_member: Any,
) -> None:
    """В разметку не должны попадать сырой Jinja и неотрендеренные ``t('…')``.

    Отдельно сторожим «голый ключ перевода»: ``t()`` на промахе печатает сам
    ключ, и на экране появляется ``friends_title`` вместо заголовка.
    """
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    ru = _load("ru")
    # Кандидаты в «голые ключи»: то, что реально используют шаблоны.
    used: set[str] = set()
    for path in TEMPLATES_DIR.rglob("*.html"):
        used.update(_T_CALL.findall(path.read_text(encoding="utf-8")))
    # Ключ считаем протёкшим, только если он существует в таблице (иначе это
    # просто похожий на ключ идентификатор в inline-JS) и при этом виден в
    # разметке как отдельное слово между тегами.
    watched = sorted(k for k in used if k in ru and "_" in k)

    failures: list[str] = []
    for path in _member_pages():
        body = (await client.get(path, follow_redirects=False)).text
        for marker in ("{{", "{%", "t('", 't("'):
            # inline-<script> легально содержит и {{, и t( — вырезаем скрипты.
            stripped = re.sub(r"<script.*?</script>", "", body, flags=re.S)
            if marker in stripped:
                failures.append(f"{path}: сырой шаблонный маркер {marker!r}")
        for key in watched:
            if re.search(rf">\s*{re.escape(key)}\s*<", body):
                failures.append(f"{path}: непереведённый ключ {key!r}")
    assert not failures, "\n".join(failures)


@pytest.mark.asyncio
async def test_member_pages_show_an_empty_state_with_a_next_step(
    fresh_member: Any,
) -> None:
    """Пустой аккаунт видит объяснение и следующий шаг, а не пустую панель."""
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    failures: list[str] = []
    for path, marker in EMPTY_STATE_PAGES.items():
        r = await client.get(path, follow_redirects=False)
        if r.status_code != 200:
            failures.append(f"{path} → {r.status_code}")
            continue
        if f'data-empty-state="{marker}"' not in r.text:
            failures.append(f"{path}: нет пустого состояния data-empty-state={marker}")
    assert not failures, "\n".join(failures)


@pytest.mark.asyncio
async def test_no_rendered_href_takes_a_member_out_of_their_surface(
    fresh_member: Any,
) -> None:
    """Ни одна нарисованная участнику ссылка не ведёт в 403/редирект на /chat.

    Allowlist строится из ``_MEMBER_PREFIXES`` + ``_PUBLIC_PREFIXES``
    (импортированы из гейта, не скопированы), поэтому тест не может
    «разрешить» путь, который гейт закрывает.
    """
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    offenders: list[str] = []
    for path in _member_pages():
        body = (await client.get(path, follow_redirects=False)).text
        for href in sorted(_hrefs(body)):
            if href.startswith(_SKIP_HREF_PREFIXES):
                continue
            if not _member_reachable(href):
                offenders.append(f"{path} рисует {href} — вне member-зоны")
    assert not offenders, "\n".join(offenders)


@pytest.mark.asyncio
async def test_member_links_actually_resolve_not_redirect_to_chat(
    fresh_member: Any,
) -> None:
    """Тот же обход, но по факту: каждый href реально отдаёт страницу.

    Статическая проверка префиксов ловит owner-ссылку, а эта — ссылку, которая
    формально внутри зоны, но роут её не отдаёт (переименовали, удалили,
    охраняют отдельной зависимостью).
    """
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    seen: dict[str, str] = {}
    offenders: list[str] = []
    for path in _member_pages():
        body = (await client.get(path, follow_redirects=False)).text
        for href in sorted(_hrefs(body)):
            if href.startswith(_SKIP_HREF_PREFIXES) or href == path:
                continue
            if href not in seen:
                r = await client.get(href, follow_redirects=False)
                loc = r.headers.get("location", "")
                seen[href] = f"{r.status_code} {loc}".strip()
            status = seen[href]
            code = status.split(" ", 1)[0]
            if code in {"403", "404", "500"}:
                offenders.append(f"{path} → {href} = {status}")
            elif code in {"302", "303"} and status.endswith(("/chat", "/landing")):
                offenders.append(f"{path} → {href} = {status} (выкинут из зоны)")
    assert not offenders, "\n".join(offenders)


@pytest.mark.asyncio
async def test_member_shell_never_advertises_the_owner_surface(
    fresh_member: Any,
) -> None:
    """Оболочка (навбар, чип аккаунта, баннеры) — без owner-маршрутов.

    Ссылка на owner-страницу не обязана быть видимой, чтобы вредить: она в
    DOM, её находит поиск по странице и «открыть в новой вкладке», а клик
    возвращает человека на /chat без объяснений.
    """
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    forbidden = (
        'href="/now"',
        'href="/timeline"',
        'href="/root"',
        'href="/m"',
        'href="/ask"',
        'href="/billing"',
        'href="/whats-new"',
        'href="/settings/system-monitor"',
        'href="/ai-activity"',
        'href="/feature-index"',
        'href="/settings"',
    )
    offenders: list[str] = []
    for path in _member_pages() + ["/help", "/help/legacy-shortcuts", "/changelog"]:
        r = await client.get(path, follow_redirects=False)
        if r.status_code != 200:
            continue
        for marker in forbidden:
            if marker in r.text:
                offenders.append(f"{path}: {marker}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.asyncio
async def test_public_marketing_pages_send_a_logged_in_member_somewhere_real(
    fresh_member: Any,
) -> None:
    """Кнопка «В кабинет» на публичных страницах не должна отскакивать.

    Она вела на /now — ленту ВЛАДЕЛЬЦА. Участник, зашедший на /pricing или
    /blog из письма или поиска, жал единственную заметную кнопку и получал
    редирект гейта назад на /chat: снаружи это выглядит как сломанный вход.
    """
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    for path in ("/landing", "/pricing", "/features", "/blog", "/help/connect-llm"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        assert 'href="/now"' not in r.text, f"{path} зовёт участника на /now"
        for href in sorted(_hrefs(r.text)):
            if href.startswith(_SKIP_HREF_PREFIXES):
                continue
            assert _member_reachable(href), f"{path} рисует участнику {href}"


@pytest.mark.asyncio
async def test_public_app_shell_pages_do_not_leak_owner_nav(
    fresh_member: Any,
) -> None:
    """/help и /changelog лежат под ПУБЛИЧНЫМ префиксом.

    Гейт на них не резолвит личность, поэтому base.html без подсказки
    ``shell_public`` считал зрителя владельцем и рисовал анониму из интернета
    полный owner-навбар (/root, /timeline, пилюля захвата).
    """
    client, _owner_user, _member_user = fresh_member
    client.cookies.clear()  # аноним

    for path in ("/help", "/help/legacy-shortcuts", "/changelog"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code == 200, f"{path} → {r.status_code}"
        for marker in ('href="/root"', 'href="/timeline"', 'href="/now"', 'href="/m"'):
            assert marker not in r.text, f"{path} показывает анониму {marker}"


# ── Онбординг → первый успех ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onboarding_and_chat_agree_on_how_to_connect_a_model(
    fresh_member: Any,
) -> None:
    """Оба экрана первого визита ведут к модели и называют бесплатные пути.

    Единственный сценарий, который обязан сработать за две минуты: человек
    видит, что модель нужна своя, что это бесплатно и куда нажать. Если
    /onboarding и пустой /chat разойдутся в формулировках, пропустивший
    онбординг останется без объяснения.
    """
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    for path in ("/onboarding", "/chat"):
        body = (await client.get(path, follow_redirects=False)).text
        assert 'href="/settings/llm"' in body, f"{path}: нет пути к подключению модели"
        assert 'href="/help/connect-llm"' in body, f"{path}: нет инструкции"
        for provider in ("OpenRouter", "Groq", "Ollama"):
            assert provider in body, f"{path}: не назван бесплатный вариант {provider}"


@pytest.mark.asyncio
async def test_member_help_is_not_the_owner_runbook(fresh_member: Any) -> None:
    """/help участнику — его справка, а не ops-раннбук владельца."""
    client, _owner_user, member_user = fresh_member
    await _as(client, member_user["id"])

    body = (await client.get("/help", follow_redirects=False)).text
    assert 'href="/chat"' in body
    assert "/admin/" not in body
    assert "devtunnel" not in body
    assert "persona.db" not in body


# ── Инварианты, которые держат тест честным ────────────────────────────────


def test_allowlist_is_derived_from_the_gate_not_copied() -> None:
    """Источник правды — гейт. Если префиксы разъедутся, тест обязан упасть."""
    assert "/chat" in _MEMBER_PREFIXES
    assert "/friends" in _MEMBER_PREFIXES
    assert "/messages" in _MEMBER_PREFIXES
    assert "/help" in _PUBLIC_PREFIXES
    assert _member_reachable("/settings/memory") is True
    assert _member_reachable("/timeline") is False
    assert _member_reachable("/root") is False
