"""Executable architecture and startup budgets.

The legacy route layer is intentionally migrated incrementally.  These tests
therefore freeze its current direct-database debt while making the new clean
layers strict from their first module.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.routing import APIRoute

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PROJECT_ROOT / "app"

# Exact measured debt before the strangler migration. New paths are forbidden;
# deleting or migrating an existing path is allowed without updating the file.
ROUTE_DIRECT_DB_IMPORT_BASELINE = frozenset(
    line.strip()
    for line in (
        PROJECT_ROOT / "tests" / "architecture_route_db_debt.txt"
    ).read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)

# The reviewed outbound browser worker adds five token-authenticated protocol
# routes to the former 1,081-route surface. Any further growth needs review.
# Reviewed +1 for the consolidated two-phase PC-worker enrollment endpoint.
# Owner ticket issuance reuses /settings/automation, so onboarding adds only
# this single unauthenticated-but-ticket-gated route.
# Reviewed +6 owner-scoped Persona surfaces: Telegram people (2), site copilot
# SSE (1), and adaptive prompt history/toggle/rollback (3).
# Reviewed +2 owner-only writes on the existing /settings/telegram-people page:
# saving a person's display name/note/mute flag, and reassigning Telegram
# ownership. Both are POSTs under a page that already existed; no new surface.
# Reviewed +1: finished month-old orphaned work — POST /api/settings/ai-search
# (owner-gated, AI-everywhere-gated, LLM-optional settings-palette fallback).
# Reviewed +4: the thinking-loop settings page and diary (GET/POST
# /settings/thinking, GET /thoughts, POST /thoughts/{id}/confirm) — all
# owner-gated, surfacing Task 1-4's thinking loop for the first time.
# Reviewed +2: GET/POST /settings/telegram-chats — the owner's UI for
# per-chat reply/read/ignore mode and history-ingest flag (telegram_chat_pref,
# Task 1-2 of the same plan), replacing the old "is it pinned in Telegram"
# accident with an explicit owner choice.
# Reviewed +1: POST /api/settings/ui-language — member-safe близнец owner-only
# POST /settings/ui-language. Участник меняет язык интерфейса ТОЛЬКО себе
# (user_settings), владелец — глобальный kv как раньше. Отдельный путь нужен
# потому, что /settings (owner-зона) участнику закрыт гейтом.
# Reviewed +15: социальный слой (друзья + личные сообщения) — первая
# поверхность, где зарегистрированные аккаунты видят ДРУГ ДРУГА, а не только
# свои данные. /friends (страница, поиск, заявка, accept/decline/cancel,
# remove, тумблер discoverable) = 8; /messages (список, ветка, открыть-с-другом,
# send, poll, older, unread.json) = 7. Консолидировать не во что: существующей
# межпользовательской поверхности в приложении не было.
# Reviewed +6: «одолжить свою модель другу» (/settings/llm/sharing) — страница,
# выдача доступа, правка лимита, пауза, отзыв и 308-редирект с хвостового
# слэша. Консолидировать не во что: /settings/llm — это МОЙ провайдер и МОЙ
# ключ (одна форма, один POST), а здесь другой объект (выдачи другим людям,
# со своим состоянием и своими лимитами) и другой набор действий; впихнуть их
# в ту же форму значило бы смешать «моя настройка» и «права других людей» на
# одном экране — ровно то, что потом читается неправильно и раздаётся лишнее.
# Reviewed +8: ИИ-ответы в личных сообщениях и уведомления социального слоя.
# /api/messages/{id}/ai (чтение настройки+черновика, запись, «убрать
# черновик») + /api/messages/ai/off-everywhere = 4; страница уведомлений
# (GET/POST /settings/notifications-social), её опрос очереди
# (/api/social-notif/pending) и проверка своего Telegram-бота
# (/api/social-notif/telegram/test) = 4. Консолидировать не во что:
# /api/messages/* — это СООБЩЕНИЯ (их чтение и отправка), а здесь настройка
# «отвечать за меня» и приватный черновик, который в dm_message не попадает
# вовсе; сунуть их в /send значило бы, что запрос на отправку сообщения умеет
# менять правила автоответов. Уведомления — отдельная от переписки поверхность
# (каналы, свой бот, антиспам почты), и её опрос дренирует уже существующий
# таймер unreadStore, а не заводит второй поллер.
# Биллинг Robokassa (2026-08-25) НЕ добавил ни одного роута: интеграция
# сознательно оставлена спящей библиотекой (app/billing/robokassa.py), продавать
# нечего — ни одна фича из PRO_FEATURES в коде не гейтится. Три ручки
# /billing/robokassa/{result,success,fail} были написаны и удалены в тот же день;
# см. docs/BILLING_ROBOKASSA.md. Значение ниже ПЕРЕМЕРЕНО на живом create_app()
# после удаления, а не получено вычитанием: параллельная работа того же дня
# добавила свои роуты, и «минус три» дало бы неверное число.
# Reviewed +3 (2026-08-25): первосторонняя аналитика владельца.
#   GET  /root/analytics           — дашборд (регистрации, воронка, страницы);
#   POST /root/analytics/settings  — рубильник сбора и окно хранения сырья;
#   POST /api/track                — приёмник кликов/сабмитов от static/track.js.
# Консолидировать не во что. /root — это ПУЛЬТ (живые логи, опрос каждые
# несколько секунд, его держат открытым при аварии), а аналитика — тяжёлое
# агрегатное чтение со свёрткой суток; в одной странице пульт платил бы за
# свёртку 30 дней при каждом открытии. Приёмник кликов нельзя слить ни с чем:
# просмотры страниц берутся из middleware, а нажатие на кнопку сервер не видит
# в принципе, и это ЕДИНСТВЕННАЯ клиентская ручка (события шлются пачкой, а не
# по одному запросу на клик). Рубильник — POST под уже существующей страницей.
# Значение ПЕРЕМЕРЕНО на живом create_app() (1157), а не получено сложением:
# в тот же день параллельно шла работа над поддержкой со своими роутами, и
# «+3 к 1149» дало бы неверное число.
#
# Reviewed +5 (2026-08-25): ПОДДЕРЖКА — «любой может написать владельцу».
# Те самые роуты, из-за которых число выше перемеряли: GET/POST /support
# (публичная форма, открыта анониму) = 2; GET/POST /settings/support
# (owner-ящик) = 2; GET /api/support/unread.json (бейдж) = 1. Значение 1_157
# уже включает их — оно снято с живого create_app() ПОСЛЕ регистрации обоих
# роутеров, поэтому здесь добавлено обоснование, а не ещё одно слагаемое.
# Консолидировано всё, что можно: карточка обращения живёт на /settings/support
# как ?ticket=N, а не отдельным /settings/support/{id} (ящик маленький,
# «открыть» = тот же список с раскрытым письмом), и все четыре действия
# владельца (прочитано / отвечено / закрыто / ответ / удалить) сведены в ОДИН
# POST с полем action — они меняют одно обращение в одной форме и отличались
# бы только строкой статуса. Слить с чем-то существующим нечего: публичной
# ручки записи от анонима в приложении не было вообще, а бейдж владельца
# нельзя подмешать в member-опросы (/api/social-notif/pending) — те
# принадлежат участнику, и ветка «а если владелец» в member-ответе это ровно
# тот механизм, из-за которого потом утекает лишнее.
#
# Reviewed +6 (2026-08-25): БЛОГ — переход от 28 статей к корпусу ~350 и
# индексируемая таксономия. Пять роутов в app/web/routes/blog.py
# (GET /blog/search, /blog/rss.xml, /blog/atom.xml, /blog/category/{slug},
# /blog/tag/{slug}) и один в app/web/routes/sitemap.py
# (GET /sitemap-{slug}.xml).
#
# Почему это не «ещё пять страниц блога», а исправление дыры. Фильтр по
# категориям в блоге был КЛИЕНТСКИЙ: кнопки на /blog прятали карточки через
# JS, и никакого URL у категории не существовало. Для поискового робота это
# означает, что страниц категорий нет вовсе — 350 статей висят как сироты под
# одним листингом, а хабовой страницы, которая собирала бы кластер и получала
# бы на себя ссылки, не существует. То же с тегами: они разбирались во
# front matter с самого начала и не выводились никуда. Роуты
# /blog/category/{slug} и /blog/tag/{slug} — это не новая функциональность,
# это перевод уже существующей группировки из JS в адресуемые страницы, без
# которых остальная работа над блогом (перелинковка, sitemap, хлебные крошки)
# не имеет во что упереться.
#
# Поиск. /blog/search — серверный, работает без JS, и он же обслуживает живой
# поиск на странице через ?format=json. Отдельной ручки /blog/search.json
# СОЗНАТЕЛЬНО НЕТ: это один и тот же запрос с одним и тем же ранжированием, и
# два роута означали бы две реализации, которые рано или поздно разъедутся в
# порядке выдачи — при том, что бюджет роутов и так на нуле. Ленты RSS и Atom
# — два роута, а не один параметризованный: их адреса (/blog/rss.xml,
# /blog/atom.xml) публикуются в <link rel="alternate">, попадают в чужие
# читалки и агрегаторы и после этого не переезжают; экономить один роут ценой
# нестабильного адреса ленты — плохой размен.
#
# Sitemap. /sitemap.xml стал индексом, а дочерние карты (pages, posts,
# categories, tags, public) обслуживает ОДИН параметризованный роут
# /sitemap-{slug}.xml, а не пять литеральных — это ровно та консолидация,
# которой требует конвенция этого файла: пять обработчиков отличались бы
# только источником списка URL. Неизвестная секция отдаёт 404, а не пустой
# urlset. Разделение сделано сейчас, хотя 350 URL помещаются и в один файл:
# после того как поисковик запомнил адрес карты, её перестройка стоит
# переоткрытия всех URL под ней.
#
# Значение ПЕРЕМЕРЕНО на живом create_app() (1163), а не получено сложением:
# в тот же день параллельно шла работа над шаблонами блога и контентом, и
# «+6 к 1157» могло бы разойтись с реальностью.
REGISTERED_ROUTE_BUDGET = 1_163  # +6 (2026-08-24): AI-everywhere wave 2 (insights, suggest, hour summary, ai-calendar parse+create); +15 (2026-08-24): social layer; +6 (2026-08-24): llm sharing grants; +8 (2026-08-24): DM AI replies + social notifications; +3 (2026-08-25): owner analytics; +5 (2026-08-25): поддержка (форма + ящик + бейдж); +6 (2026-08-25): блог — поиск, ленты, категории, теги, sitemap-индекс

# After the first bootstrap extraction the reference Windows host imports the
# web app in ~5.3 s. Keep CI headroom while preventing a return to the measured
# 11.6 s legacy baseline. The master-plan target remains <2 s.
COLD_IMPORT_BUDGET_SECONDS = 10.0

_DB_ENTRYPOINTS = frozenset({"get_connection", "write_transaction"})


def _python_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return ()
    return root.rglob("*.py")


def _imports(path: Path) -> list[tuple[int, str, frozenset[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, frozenset[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                relative = path.relative_to(PROJECT_ROOT).with_suffix("")
                package = ("app", *relative.parts[1:-1])
                keep = max(0, len(package) - node.level + 1)
                prefix = ".".join(package[:keep])
                module = ".".join(part for part in (prefix, module) if part)
            found.append(
                (
                    node.lineno,
                    module,
                    frozenset(alias.name for alias in node.names),
                )
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, frozenset()))
    return found


def _matches_prefix(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def test_new_domain_and_application_layers_obey_dependency_rule() -> None:
    """Clean layers depend inward and never acquire framework/infrastructure."""
    rules = {
        APP_ROOT / "domains": (
            "aiosqlite",
            "fastapi",
            "httpx",
            "jinja2",
            "playwright",
            "requests",
            "starlette",
            "telegram",
            "app.adapters",
            "app.application",
            "app.bootstrap",
            "app.entrypoints",
            "app.integrations",
            "app.llm",
            "app.mcp",
            "app.settings",
            "app.storage",
            "app.web",
        ),
        APP_ROOT / "application": (
            "aiosqlite",
            "fastapi",
            "httpx",
            "jinja2",
            "playwright",
            "requests",
            "starlette",
            "telegram",
            "app.adapters",
            "app.bootstrap",
            "app.entrypoints",
            "app.integrations",
            "app.llm",
            "app.mcp",
            "app.settings",
            "app.storage",
            "app.web",
        ),
    }
    violations: list[str] = []
    for layer_root, forbidden in rules.items():
        assert layer_root.is_dir(), f"Required clean layer is missing: {layer_root}"
        assert any(_python_files(layer_root)), f"Clean layer has no Python modules: {layer_root}"
        for path in _python_files(layer_root):
            for line, module, names in _imports(path):
                candidates = (module, *(f"{module}.{name}" for name in names))
                if any(_matches_prefix(candidate, forbidden) for candidate in candidates):
                    rel = path.relative_to(PROJECT_ROOT).as_posix()
                    violations.append(f"{rel}:{line} imports {module}")

    assert not violations, "Dependency rule violations:\n" + "\n".join(violations)


def test_legacy_routes_do_not_increase_direct_database_import_debt() -> None:
    """The legacy baseline may shrink, but new direct DB route imports are blocked."""
    violating_files: set[str] = set()
    for path in _python_files(APP_ROOT / "web" / "routes"):
        if any(
            module == "app.storage.db" and bool(names & _DB_ENTRYPOINTS)
            for _line, module, names in _imports(path)
        ):
            violating_files.add(path.relative_to(PROJECT_ROOT).as_posix())

    unexpected = violating_files - ROUTE_DIRECT_DB_IMPORT_BASELINE
    assert not unexpected, (
        "A new route acquired a direct database import. Move its SQL behind "
        "an application port/adapter. New violations:\n"
        + "\n".join(sorted(unexpected))
    )


def _registered_app():
    # Deliberately lazy: collection of unrelated tests must not pay the current
    # ~11 s web import cost merely because this module was discovered.
    from app.web.main import app  # noqa: PLC0415

    return app


def test_fastapi_method_path_pairs_are_unique() -> None:
    """FastAPI's first-match behaviour must never shadow another handler."""
    app = _registered_app()
    handlers: dict[tuple[str, str], list[str]] = defaultdict(list)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        endpoint = route.endpoint
        identity = (
            f"{getattr(endpoint, '__module__', '<unknown>')}."
            f"{getattr(endpoint, '__name__', route.name)}"
        )
        for method in route.methods:
            handlers[(method, route.path)].append(identity)

    duplicates = {
        f"{method} {path}": endpoints
        for (method, path), endpoints in handlers.items()
        if len(endpoints) > 1
    }
    assert not duplicates, "Duplicate FastAPI routes:\n" + json.dumps(
        duplicates, indent=2, ensure_ascii=False
    )


def test_collision_fixes_keep_the_intended_handlers_reachable() -> None:
    app = _registered_app()
    get_handlers = {
        route.path: route.endpoint
        for route in app.routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    }

    expected = {
        "/features": "app.web.routes.landing",
        "/feature-index": "app.web.routes.feature_index",
        "/changelog": "app.web.routes.changelog",
        "/roadmap/releases": "app.web.routes.landing",
        "/help/shortcuts": "app.web.routes.shortcuts_help",
        "/help/legacy-shortcuts": "app.web.routes.help",
    }
    missing_or_wrong = {
        path: getattr(get_handlers.get(path), "__module__", None)
        for path, module in expected.items()
        if getattr(get_handlers.get(path), "__module__", None) != module
    }
    assert not missing_or_wrong, f"Canonical route handlers changed: {missing_or_wrong}"


def test_registered_route_count_stays_within_budget() -> None:
    count = len(_registered_app().routes)
    assert count <= REGISTERED_ROUTE_BUDGET, (
        f"Registered route budget exceeded ({count} > {REGISTERED_ROUTE_BUDGET}). "
        "Consolidate an existing surface or explicitly review and ratchet the budget."
    )


def test_cold_web_import_stays_within_regression_budget() -> None:
    """Measure a real fresh interpreter, not an already-warm pytest import."""
    marker = "PERSONA_STARTUP_PROBE:"
    script = (
        "import json,time;"
        "started=time.perf_counter();"
        "import app.web.main as main;"
        "elapsed=time.perf_counter()-started;"
        f"print('{marker}'+json.dumps({{'seconds':elapsed,'routes':len(main.app.routes)}}))"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=COLD_IMPORT_BUDGET_SECONDS + 15,
        check=False,
    )
    assert completed.returncode == 0, (
        "Cold import process failed:\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    probe_line = next(
        (line for line in completed.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    assert probe_line is not None, f"Startup probe produced no result:\n{completed.stdout}"
    result = json.loads(probe_line.removeprefix(marker))
    assert result["seconds"] <= COLD_IMPORT_BUDGET_SECONDS, (
        "Cold app.web.main import exceeded budget "
        f"({result['seconds']:.2f}s > {COLD_IMPORT_BUDGET_SECONDS:.2f}s)."
    )
    assert result["routes"] <= REGISTERED_ROUTE_BUDGET
