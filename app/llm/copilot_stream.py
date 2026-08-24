"""Стрим встроенного копилота Persona (SSE-события delta/meta/done).

Слайс B2. Копилот живёт прямо на сайте (панель справа снизу под мастер-флагом
«ИИ везде») и отвечает на три вида запросов:

* ``ask``          — обычный вопрос про Persona / жизнь пользователя. Подмешиваем
                     короткий контекст: recall памяти по всем чатам (если есть).
* ``summary``      — кратко объясни/суммируй текущую страницу (по ``page_url``).
* ``find_setting`` — намерение пользователя → текстом, какую страницу настроек
                     открыть. Сначала пробуем чистую функцию
                     :func:`app.web.routes.settings_hub.search_settings`.

Паттерн SSE полностью повторяет :mod:`app.llm.qa_stream`: генератор yield-ит
dict-ы с ключом ``type`` (``meta`` → 0..N ``delta`` → ``done``), а HTTP-слой
(:mod:`app.web.routes.copilot`) сериализует их в кадры ``data: <json>\\n\\n``.
Ошибку конфигурации LLM отдаём отдельным событием ``{type:'error',
reason:'llm_offline'}`` — сервер не должен 500-ить, если ПК-воркер офлайн.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.llm.client import CompletionRequest, LLMClient, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import set_kv

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.copilot_stream")

#: Единый характер копилота. Коротко, по делу, по-русски — он помогает прямо на
#: странице, а не пишет эссе.
_COPILOT_SYSTEM = (
    "Ты — встроенный копилот Persona, помогаешь пользователю прямо на сайте, "
    "коротко и по делу, по-русски."
)

#: Характер копилота для УЧАСТНИКА (зарегистрированный не-владелец).
#:
#: Отличий от владельческого два, и оба про границу данных:
#: 1. явно сказано, что кроме СВОИХ данных у копилота ничего нет — чтобы модель
#:    не выдумывала «твой захват экрана» и «твои напоминания», которых у
#:    участника физически не существует (это поверхность владельца);
#: 2. запрет ссылаться на пути вне member-каталога: owner-only URL участнику
#:    отдаётся редиректом на /chat, и совет «зайди в /settings/capture» —
#:    просто ложь.
_MEMBER_SYSTEM = (
    "Ты — встроенный копилот Persona, помогаешь пользователю прямо на сайте, "
    "коротко и по делу, по-русски.\n"
    "Перед тобой участник Persona: у него свой аккаунт, своя подключённая "
    "модель, своя память и свои настройки. Никаких чужих данных (в том числе "
    "владельца инстанса) у тебя нет — не выдумывай их и не обещай.\n"
    "Захвата экрана, микрофона, таймлайна, заметок и напоминаний у него НЕТ: "
    "это поверхность владельца инстанса, не предлагай их.\n"
    "Ссылайся ТОЛЬКО на пути из списка «Страницы, которые ему доступны» ниже. "
    "Любой другой путь ему закрыт — совет открыть его будет неправдой."
)

#: Открытые участнику страницы ВНЕ каталога настроек (_MEMBER_CATEGORIES).
#: Держим списком здесь, а не в хабе: это не настройки, а рабочие экраны.
#: Источник истины по доступу — ``_MEMBER_PREFIXES`` в middleware/auth_gate.py.
_MEMBER_EXTRA_PAGES: tuple[tuple[str, str], ...] = (
    ("/chat", "Чат с ИИ и памятью — главный экран"),
    ("/voice", "Голосовой разговор"),
    ("/onboarding", "Быстрый старт: подключить модель"),
    ("/settings/hub", "Все настройки одним списком"),
    ("/help/connect-llm", "Как бесплатно получить ключ для модели"),
)

#: Допустимые режимы. Неизвестный режим тихо трактуем как обычный вопрос,
#: чтобы кривой ?mode= не ронял стрим.
_VALID_MODES = ("ask", "summary", "find_setting")

#: Единственный кадр для «у пользователя нет своей модели». Форма закреплена
#: слайсом 1 (``reason='llm_not_configured'``); ``href`` даёт UI кликабельную
#: ссылку, а не путь внутри текста. Один объект на оба пути — предварительную
#: проверку в роуте и мягкую обработку внутри стрима, чтобы они не разъезжались.
LLM_NOT_CONFIGURED_EVENT: dict[str, Any] = {
    "type": "error",
    "reason": "llm_not_configured",
    "message": "Свой AI не подключён — открой /settings/llm",
    "href": "/settings/llm",
}

#: Бюджет ответа — копилот отвечает кратко, длинные простыни тут не нужны.
_MAX_TOKENS = 500
_ENABLE_MARKERS = ("включи", "включить", "активируй", "enable", "turn on")
_DISABLE_MARKERS = ("выключи", "отключи", "disable", "turn off")


async def _recall_context(question: str, user_id: int | None) -> str:
    """Короткий блок памяти по всем чатам пользователя (best-effort).

    Использует keyword-recall (FTS5 bm25 + LIKE-fallback) — тот же путь, что и
    чат по умолчанию. Любой сбой (нет таблиц/пустая память) → пустая строка,
    копилот просто ответит без контекста.
    """
    if not user_id or not question:
        return ""
    try:
        from app.chat.sessions import recall_relevant  # noqa: PLC0415

        return (await recall_relevant(user_id, question, limit=4)) or ""
    except Exception as exc:  # noqa: BLE001 — память опциональна, не роняем стрим
        log.debug("copilot.recall_failed", error=str(exc))
        return ""


async def _is_member(user_id: int | None) -> bool:
    """True — не-владелец (ему отдаём ТОЛЬКО member-каталог настроек).

    ``None`` (фоновый/внутренний вызов) считаем владельцем: так ведёт себя
    весь остальной код. Сбой резолва → участник, чтобы ошибка гейта не
    открыла owner-only ссылки.
    """
    if user_id is None:
        return False
    try:
        from app.auth.owner import is_owner  # noqa: PLC0415

        return not await is_owner(int(user_id))
    except Exception:  # noqa: BLE001 — сбой гейта → урезанный каталог
        return True


def _find_settings_block(question: str, *, member: bool = False) -> str:
    """Топ-совпадения по страницам настроек для режима ``find_setting``.

    Чистая функция :func:`search_settings` не ходит в БД, поэтому зовём её
    синхронно. Возвращаем компактный список «label → href» для подсказки LLM
    (или пустую строку, если ничего не нашлось).

    ``member=True`` ищет по урезанному каталогу участника: иначе копилот
    подсказывал бы не-владельцу ссылки на owner-only страницы (захват, OCR,
    диагностика), которых он даже открыть не может.
    """
    try:
        from app.web.routes.settings_hub import search_settings  # noqa: PLC0415

        results = search_settings(question, limit=6, member=member)
    except Exception as exc:  # noqa: BLE001 — поиск опционален
        log.debug("copilot.search_settings_failed", error=str(exc))
        return ""
    lines = [f"• {r['label']} → {r['href']}" for r in results]
    return "\n".join(lines)


def _member_pages() -> list[tuple[str, str]]:
    """Плоский список «путь → подпись» всего, что открыто участнику.

    Источник — member-каталог настроек (``_MEMBER_CATEGORIES``) плюс рабочие
    экраны из :data:`_MEMBER_EXTRA_PAGES`. Owner-only путей тут физически нет,
    поэтому промпт, собранный из этого списка, не может посоветовать участнику
    страницу, которую гейт всё равно закроет.
    """
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        from app.web.routes.settings_hub import _categories_json  # noqa: PLC0415

        for cat in _categories_json(member=True):
            for page in cat["pages"]:  # type: ignore[index]
                href = str(page["href"])
                if href in seen:
                    continue
                seen.add(href)
                pages.append((href, str(page["label"])))
    except Exception as exc:  # noqa: BLE001 — каталог опционален, не роняем стрим
        log.debug("copilot.member_catalog_failed", error=str(exc))
    for href, label in _MEMBER_EXTRA_PAGES:
        if href not in seen:
            seen.add(href)
            pages.append((href, label))
    return pages


def _member_pages_block() -> str:
    """Тот же список, но готовым текстом для системного промпта участника."""
    lines = [f"• {href} — {label}" for href, label in _member_pages()]
    return "\n".join(lines)


def _normalize_path(page_url: str) -> str:
    """``/settings/theme?x=1#y`` → ``/settings/theme`` (для сверки с каталогом)."""
    path = str(page_url or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not path:
        return ""
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _member_page_label(page_url: str) -> str:
    """Подпись страницы участника по URL (или '' — страница вне его зоны)."""
    path = _normalize_path(page_url)
    if not path:
        return ""
    for href, label in _member_pages():
        if href == path:
            return label
    return ""


def _system_prompt(*, member: bool) -> str:
    """Системный промпт под роль. Владельцу — прежняя строка, байт-в-байт."""
    if not member:
        return _COPILOT_SYSTEM
    pages = _member_pages_block()
    if not pages:
        return _MEMBER_SYSTEM
    return f"{_MEMBER_SYSTEM}\n\nСтраницы, которые ему доступны:\n{pages}"


async def _personal_memory_block(user_id: int | None) -> str:
    """Личная память УЧАСТНИКА (``user_memory``, строго по его ``user_id``).

    Best-effort: нет таблицы/пусто/ошибка → пустая строка. Ничего глобального
    тут не читается, поэтому чужие факты сюда попасть не могут.
    """
    if not user_id:
        return ""
    try:
        from app.chat.user_memory import build_memory_block  # noqa: PLC0415

        return (await build_memory_block(int(user_id), max_items=8)) or ""
    except Exception as exc:  # noqa: BLE001 — память опциональна
        log.debug("copilot.user_memory_failed", error=str(exc))
        return ""


# ── Действия участника: пишем в ЕГО user_settings, не в глобальный kv ────────

#: Слова, по которым узнаём просьбу про тему оформления.
_THEME_MARKERS = ("тем", "theme", "оформлен")

#: …и обязательный глагол-приказ. Без него «какая у меня тема в Persona?»
#: попадало бы под маркеры «тем» + «persona» и ВОПРОС молча переключал бы
#: человеку оформление. Действие применяется только на явную просьбу.
_THEME_VERBS = (
    *_ENABLE_MARKERS,
    "сделай", "сделать", "поставь", "поставить", "смени", "сменить",
    "поменяй", "поменять", "переключи", "переключить", "set ", "switch",
)

#: Значение темы → маркеры. Порядок важен: «тёмный космос» должен стать
#: ``cosmos-dark``, а не ``dark``, поэтому составные варианты идут первыми.
#: Набор значений — 1:1 с ``_VALID_THEMES`` в app/web/routes/theme.py.
_THEME_VALUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cosmos-dark", ("cosmos dark", "космос тёмн", "космос темн",
                     "тёмный космос", "темный космос")),
    ("cosmos", ("cosmos", "космос")),
    ("persona", ("persona", "персона")),
    ("auto", ("авто", "auto", "системн", "как в системе")),
    ("light", ("светл", "light")),
    ("dark", ("тёмн", "темн", "dark")),
)

#: Человеческие названия тем для ответа копилота.
_THEME_LABELS = {
    "dark": "тёмную",
    "light": "светлую",
    "auto": "системную (auto)",
    "persona": "Persona",
    "cosmos": "Cosmos",
    "cosmos-dark": "Cosmos Dark",
}


def _match_theme(lowered: str) -> str | None:
    """Какую тему ПРОСИТ переключить участник (``None`` → это не просьба)."""
    if not any(marker in lowered for marker in _THEME_MARKERS):
        return None
    if not any(verb in lowered for verb in _THEME_VERBS):
        return None
    for value, markers in _THEME_VALUES:
        if any(marker in lowered for marker in markers):
            return value
    return None


async def _apply_member_setting_action(
    lowered: str, user_id: int
) -> tuple[str, str] | None:
    """Тот же класс действий, что у владельца, но по СВОИМ настройкам.

    Владелец переключает глобальные kv-строки инстанса; участнику писать туда
    нельзя (это чужие настройки), но его собственные ``user_settings`` —
    ровно его. Адреса записи те же, что у страниц настроек участника:
    ``theme`` (см. app/web/routes/theme.py) и ``advanced_mode`` / ``feat_*``
    (см. app/web/routes/advanced_settings.py) — так что применённое копилотом
    видно в форме, а не живёт отдельной невидимой жизнью.
    """
    from app.storage.repository import set_user_kv  # noqa: PLC0415

    uid = int(user_id)

    theme = _match_theme(lowered)
    if theme is not None:
        async with get_connection() as conn:
            await set_user_kv(conn, uid, "theme", theme)
        # Тема читается синхронным Jinja-глобалом через процесс-кэш: без сброса
        # следующий рендер до 15 с показывал бы старую тему, и «включил» выглядел
        # бы враньём.
        try:
            from app.web.templates_engine import (  # noqa: PLC0415
                invalidate_theme_cache,
                invalidate_user_kv_sync,
            )

            invalidate_theme_cache()
            invalidate_user_kv_sync(uid, "theme")
        except Exception as exc:  # noqa: BLE001 — кэш опционален
            log.debug("copilot.theme_cache_skip", error=str(exc))
        return f"Поставил тебе {_THEME_LABELS.get(theme, theme)} тему.", "/settings/theme"

    enabled: bool | None = None
    if any(marker in lowered for marker in _ENABLE_MARKERS):
        enabled = True
    elif any(marker in lowered for marker in _DISABLE_MARKERS):
        enabled = False
    if enabled is None:
        return None

    if "расширенн" in lowered:
        key, label = "advanced_mode", "расширенный режим"
    elif "инструмент" in lowered:
        key, label = "feat_tools", "инструменты Persona"
    else:
        return None

    async with get_connection() as conn:
        if key == "feat_tools" and enabled:
            await set_user_kv(conn, uid, "advanced_mode", "1")
        await set_user_kv(conn, uid, key, "1" if enabled else "0")
    verb = "Включил" if enabled else "Выключил"
    return f"{verb} тебе {label}.", "/settings/advanced"


async def _apply_safe_setting_action(
    question: str, user_id: int | None = None
) -> tuple[str, str] | None:
    """Apply a tiny allowlist of explicit setting requests — по адресу роли.

    Строки, в которые пишет владельческая ветка (``ai_everywhere`` /
    ``advanced_mode`` / ``feat_tools``) — ГЛОБАЛЬНЫЕ kv-флаги, общие для всего
    инстанса. Раньше гейта не было, и любой зарегистрированный пользователь
    фразой «включи инструменты» переключал флаги ВЛАДЕЛЬЦА; потом участнику
    просто запретили действие целиком.

    Теперь роль выбирает АДРЕС записи, а не наличие фичи: владелец пишет
    глобальный kv (ровно как раньше), участник — свою строку в
    ``user_settings`` (:func:`_apply_member_setting_action`). Глобальные флаги
    инстанса участник по-прежнему не трогает ничем.
    """
    lowered = " ".join(str(question or "").casefold().split())

    if user_id is not None:
        try:
            from app.auth.owner import is_owner  # noqa: PLC0415

            owner = await is_owner(int(user_id))
        except Exception:  # noqa: BLE001 — сбой гейта → глобальное не трогаем
            return None
        if not owner:
            return await _apply_member_setting_action(lowered, int(user_id))

    enabled: bool | None = None
    if any(marker in lowered for marker in _ENABLE_MARKERS):
        enabled = True
    elif any(marker in lowered for marker in _DISABLE_MARKERS):
        enabled = False
    if enabled is None:
        return None

    key = ""
    label = ""
    href = ""
    if "ии везде" in lowered or "ai everywhere" in lowered:
        key, label, href = "ai_everywhere", "режим «ИИ везде»", "/settings/ai-everywhere"
    elif "расширенн" in lowered:
        key, label, href = "advanced_mode", "расширенный режим", "/settings/advanced"
    elif "инструмент" in lowered:
        key, label, href = "feat_tools", "инструменты Persona", "/settings/advanced"
    if not key:
        return None
    async with get_connection() as conn:
        if key == "feat_tools" and enabled:
            await set_kv(conn, "advanced_mode", "1")
        await set_kv(conn, key, "1" if enabled else "0")
    verb = "Включил" if enabled else "Выключил"
    return f"{verb} {label}.", href


def _build_prompt(
    question: str, page_url: str, mode: str, extra: str, *, member: bool = False
) -> str:
    """Собрать пользовательский промпт под конкретный режим копилота.

    ``member=True`` добавляет осведомлённость о странице: голый URL модель
    трактует как угодно, а подпись из member-каталога («/settings/llm —
    провайдер и ключ твоей модели») сразу говорит, что это за экран и что там
    делают. Для владельца промпт собирается ровно как раньше.
    """
    page_url = (page_url or "").strip()
    page_label = _member_page_label(page_url) if member else ""
    page_line = (
        f"Пользователь сейчас на странице {_normalize_path(page_url)} — {page_label}."
        if page_label
        else ""
    )
    if mode == "summary":
        parts = [
            "Пользователь просит кратко объяснить/суммировать текущую страницу "
            "сайта Persona.",
        ]
        if page_url:
            parts.append(f"URL страницы: {page_url}")
        if page_line:
            parts.append(page_line)
        if question:
            parts.append(f"Уточнение пользователя: {question}")
        parts.append(
            "Ответь в 2-4 предложениях: что это за экран и что тут можно "
            "сделать. Без воды."
        )
        return "\n".join(parts)

    if mode == "find_setting":
        parts = [
            "Пользователь хочет найти нужную страницу настроек Persona по "
            "своему намерению.",
            f"Намерение: {question}" if question else "Намерение: (не указано)",
        ]
        if page_line:
            parts.append(page_line)
        if extra:
            parts.append(
                "Подходящие страницы настроек (label → путь):\n" + extra
            )
            parts.append(
                "Выбери самый подходящий пункт и ответь одним-двумя "
                "предложениями: какую страницу открыть и её путь. Если ничего "
                "не подходит — так и скажи."
            )
        else:
            parts.append(
                "Точных совпадений в каталоге настроек не нашлось. Подскажи "
                "по смыслу, куда примерно смотреть, коротко."
            )
        return "\n".join(parts)

    # mode == "ask" (и любой неизвестный режим)
    parts = [f"Вопрос пользователя: {question}"]
    if page_url:
        parts.append(f"(Пользователь сейчас на странице: {page_url})")
    if page_line:
        parts.append(page_line)
    if extra:
        parts.append(
            "Что известно из памяти по прошлым чатам (может пригодиться):\n"
            + extra
        )
    parts.append("Ответь коротко и по делу.")
    return "\n".join(parts)


async def _assemble_context(
    question: str, mode: str, user_id: int | None, *, member: bool
) -> str:
    """Весь контекст, который уезжает в модель. ГРАНИЦА ДАННЫХ живёт здесь.

    Каждый источник ниже — строго per-user либо ролевой:

    * :func:`_recall_context` → ``recall_relevant(user_id, …)``: SQL фильтрует
      по ``chat_session.user_id``, чужие чаты недостижимы;
    * :func:`_personal_memory_block` → ``user_memory`` по ЕГО ``user_id``
      (только участнику — владельцу промпт остаётся прежним, байт-в-байт);
    * :func:`_find_settings_block` → member-каталог настроек при ``member``.

    Захвата экрана/OCR/аудио, часовых карточек, заметок, напоминаний и
    глобального ``chat_system_prompt`` владельца тут НЕТ ни одного источника.
    Добавлять их сюда без ролевого гейта нельзя: канареечные тесты в
    ``tests/test_copilot_member.py`` ловят именно такую утечку.
    """
    if mode == "find_setting":
        return _find_settings_block(question, member=member)
    if mode != "ask":
        return ""
    extra = await _recall_context(question, user_id)
    if not member:
        return extra
    facts = await _personal_memory_block(user_id)
    if not facts:
        return extra
    return f"{extra}\n\n{facts}".strip()


async def _not_configured_event(
    user_id: int | None, exc: Exception
) -> dict[str, Any]:
    """Кадр ошибки «нет модели», разный для владельца и обычного юзера.

    Владелец — ровно прежний кадр ``reason='llm_offline'`` («ПК-воркер
    офлайн»): его копилот и правда крутится на домашнем ПК. Остальным этот
    текст бессмысленен — им нужен свой провайдер, поэтому отдаём
    ``reason='llm_not_configured'`` со ссылкой на /settings/llm.
    """
    owner = False
    if user_id is not None:
        try:
            from app.auth.owner import is_owner  # noqa: PLC0415

            owner = await is_owner(int(user_id))
        except Exception:  # noqa: BLE001 — сбой гейта не должен ронять стрим
            owner = False
    if user_id is None or owner:
        log.info("copilot.llm_offline", error=str(exc))
        return {
            "type": "error",
            "reason": "llm_offline",
            "message": "ПК-воркер офлайн",
        }
    log.info("copilot.llm_not_configured", error=str(exc))
    return dict(LLM_NOT_CONFIGURED_EVENT)


async def stream_copilot(
    question: str,
    page_url: str = "",
    mode: str = "ask",
    user_id: int | None = None,
    client: LLMClient | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE-ready dict-ы для одного запроса копилота.

    Порядок событий как в qa_stream: ровно один ``meta`` → 0..N ``delta`` →
    ровно один ``done`` (даже если ответ пустой). При недоступном LLM отдаём
    ``{type:'error', reason:'llm_offline'}`` вместо исключения наружу.
    """
    question = (question or "").strip()
    mode = (mode or "ask").strip().lower()
    if mode not in _VALID_MODES:
        mode = "ask"

    # Каталог настроек урезаем для не-владельца (тот же гейт, что у хаба).
    member = await _is_member(user_id)

    extra = await _assemble_context(question, mode, user_id, member=member)

    yield {"type": "meta", "mode": mode, "has_context": bool(extra)}

    action = await _apply_safe_setting_action(question, user_id)
    if action is not None:
        answer, href = action
        yield {"type": "delta", "text": answer}
        yield {
            "type": "done",
            "full_answer": answer,
            "href": href,
            "label": "Открыть настройку",
        }
        return

    prompt = _build_prompt(question, page_url, mode, extra, member=member)
    request = CompletionRequest(
        system=_system_prompt(member=member),
        user=prompt,
        max_tokens=_MAX_TOKENS,
    )

    # make_client(kind='copilot') у ВЛАДЕЛЬЦА → провайдер worker (модель на
    # ПК), ключ не нужен. У обычного пользователя ``user_id`` уводит резолв в
    # его собственные настройки: чужой ПК недоступен, нужен свой провайдер.
    # Если LLM не сконфигурирован/воркер офлайн — благородная ошибка.
    try:
        llm = client or make_client(kind="copilot", user_id=user_id)
    except LLMNotConfigured as exc:
        # Разводим два случая: у ВЛАДЕЛЬЦА это «ПК-воркер офлайн» (кадр как
        # был, байт-в-байт), у обычного пользователя — «свой AI не подключён»,
        # что чинится на /settings/llm, а не ожиданием.
        error_event = await _not_configured_event(user_id, exc)
        yield error_event
        return

    chunks: list[str] = []
    try:
        async for delta in llm.stream(request):
            if not delta:
                continue
            chunks.append(delta)
            yield {"type": "delta", "text": delta}
    except LLMNotConfigured as exc:
        # Воркер мог отвалиться уже в процессе стрима (poll-таймаут/офлайн);
        # у не-владельца та же ошибка означает «твой провайдер не отвечает».
        log.info("copilot.llm_offline_mid", error=str(exc))
        yield await _not_configured_event(user_id, exc)
        return
    except Exception as exc:  # noqa: BLE001 — любой сбой стрима → мягкий done
        log.warning("copilot.stream_failed", error=str(exc))
        yield {
            "type": "done",
            "full_answer": "".join(chunks),
            "error": str(exc),
        }
        return

    full_answer = "".join(chunks)
    log.info(
        "copilot.done",
        mode=mode,
        answer_len=len(full_answer),
        has_context=bool(extra),
    )
    done: dict[str, Any] = {"type": "done", "full_answer": full_answer}
    if mode == "find_setting":
        try:
            from app.web.routes.settings_hub import search_settings  # noqa: PLC0415

            settings = search_settings(question, limit=3, member=member)
            if settings:
                done["settings"] = settings
        except Exception:
            pass
    yield done


__all__ = ["LLM_NOT_CONFIGURED_EVENT", "stream_copilot"]
