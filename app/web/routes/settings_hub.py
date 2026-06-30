"""Settings category hub (v1.27).

Aside from the kitchen-sink ``/settings`` env-grid, Persona accumulated
~26 ``/settings/*`` sub-pages with no discoverability — the only way to
reach them was the URL bar or the auto-generated feature index. This
route renders a single grid of category cards: each card groups
several existing sub-pages by topic, with a one-line description and
a direct link.

The route is **purely UI**: it does not read or write any kv setting.
All it does is render a static catalogue, so future maintenance is
just an edit to the ``_CATEGORIES`` list when a new sub-page lands.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.i18n import get_ui_language, t
from app.web.templates_engine import templates

router = APIRouter(tags=["settings-hub"])

# Синонимы для поиска: пользователь печатает «пароль» — находим api-tokens и т.п.
# Маппинг href → доп. ключевые слова (рус+eng), по которым тоже матчим.
_KEYWORDS: Final[dict[str, str]] = {
    "/settings/api-tokens": "пароль ключ доступ token api безопасность",
    "/settings/theme": "тема оформление цвет dark light внешний вид appearance",
    "/settings/voice": "голос микрофон озвучка tts stt voice речь",
    "/settings/memory": "память факты помнит memory знает обо мне",
    "/settings/system-prompt": "характер роль личность промпт persona system prompt",
    "/settings/llm": "модель провайдер ключ api llm gpt ollama claude",
    "/settings/smtp": "почта email письмо уведомления smtp",
    "/vault": "секреты пароли заметки шифрование vault",
    "/settings/backup/manage": "бэкап резерв копия восстановление backup",
    "/audit": "аудит лог история действий audit log",
    "/settings/capture": "захват скриншоты экран запись частота",
    "/settings/redaction": "секреты скрытие приватность redaction маскирование",
    "/settings/privacy": "приватность данные локально облако экспорт памяти удалить privacy local-first",
    "/chat": "чат беседа ассистент диалог chat",
    "/ai-activity": "активность инструменты что делает ии прозрачность",
    "/graph": "граф связи память узлы graph",
    "/analytics": "аналитика статистика тренды графики использование ии токены покрытие analytics",
    "/day": "день обзор дня скриншоты звук спросить про день day",
    "/briefing": "брифинг сводка дня проактивно карточки утро итоги briefing digest",
    "/settings/skills": "навыки skills инструкции github умения skill",
    "/settings/automation": "браузер browser mcp автоматизация playwright инструменты агент домены allowlist",
    "/settings/integrations": "интеграции календарь ics icalendar экспорт напоминания markdown integrations calendar",
}


_CATEGORIES: Final[list[dict[str, object]]] = [
    {
        "title_key": "settings_cat_capture_title",
        "desc_key": "settings_cat_capture_desc",
        "icon": "🎥",
        "pages": [
            ("/settings/capture", "Основное (вкл/выкл, частота, расписание)"),
            ("/settings/audio", "Расширенные параметры аудио (codec, VAD)"),
            ("/settings/blocklist", "Регекс-блоклист окон/приложений"),
            ("/settings/app-capture-skip", "Список приложений-исключений"),
            ("/settings/app-overrides", "Per-app частота захвата"),
            ("/quiet-hours", "Quiet hours (часы без захвата)"),
        ],
    },
    {
        "title_key": "settings_cat_ocr_title",
        "desc_key": "settings_cat_ocr_desc",
        "icon": "🔍",
        "pages": [
            ("/settings/ocr-languages", "Языки OCR"),
            ("/settings/ocr-skip", "Skip-list для OCR"),
            ("/settings/phrase-tags", "Фразовые правила → теги"),
            ("/settings/redaction", "Регексы для скрытия секретов в OCR"),
            ("/ocr-admin", "OCR-админка (статус, perepriem)"),
        ],
    },
    {
        "title_key": "settings_cat_memory_title",
        "desc_key": "settings_cat_memory_desc",
        "icon": "🧠",
        "pages": [
            ("/briefing", "🗞 Брифинг — проактивные карточки дня (👍/👎, тихие часы)"),
            ("/graph", "🕸 Граф памяти — промпты, ответы, записи и связи"),
            ("/settings/digest-prompt", "Промпт для дайджестов"),
            ("/memory", "Tier 1+5: hourly cards + daily pins"),
            ("/memory/weeks", "Tier 2: weekly cards"),
        ],
    },
    {
        "title_key": "settings_cat_ai_title",
        "desc_key": "settings_cat_ai_desc",
        "icon": "🤖",
        "pages": [
            ("/chat", "Чат с памятью (беседы, модель-пикер, vision)"),
            ("/ai-activity", "🔭 Что делает ИИ — окно активности (инструменты live)"),
            ("/voice", "🎙️ Голосовой разговор (hands-free, орб-микрофон)"),
            ("/settings/skills", "🧩 Навыки — наборы инструкций из GitHub (SKILL.md)"),
            ("/settings/automation", "🌐 Автоматизация — браузер-агент + MCP-рантайм (бэкенд, домены)"),
            ("/settings/advanced", "⚙️ Расширенные функции (мастер-выключатель: друг ⇄ рабочий)"),
            ("/settings/system-prompt", "Системный промпт / характер ассистента (пресеты + редактор)"),
            ("/settings/profile", "Профиль — что ИИ знает обо мне"),
            ("/settings/memory", "🧠 Память — что ИИ помнит обо мне (просмотр/правка/закрепить)"),
            ("/settings/voice", "🎙️ Голосовой ассистент (вкл/выкл, выбор чата, голос)"),
            ("/settings/mac-fs", "AI и файлы — пишет прямо на Mac (allowlist) + выбор устройства"),
            ("/ask", "Спросить — разовый вопрос по истории"),
            ("/admin/mcp", "MCP-серверы и встроенные инструменты"),
            ("/admin/dataset", "Датасет Q&A для fine-tune PersonaAI"),
            ("/settings/llm", "Провайдер + ключ LLM"),
        ],
    },
    {
        "title_key": "settings_cat_devices_title",
        "desc_key": "settings_cat_devices_desc",
        "icon": "📱",
        "pages": [
            ("/devices", "Устройства (пауза, интервал, ★ код-таргет)"),
            ("/welcome/install/mac", "Установить / обновить Mac-агент"),
            ("/welcome/install/windows", "Установить / обновить Windows-агент"),
            ("/welcome", "Онбординг — подключить новое устройство"),
            ("/sync", "Синхронизация — статус и watermark'и"),
            ("/storage", "Хранилище — квоты, retention, очистка"),
            ("/admin/agents", "Remote agents — токены ingest"),
        ],
    },
    {
        "title_key": "settings_cat_apps_title",
        "desc_key": "settings_cat_apps_desc",
        "icon": "🏷",
        "pages": [
            ("/settings/app-aliases", "Алиасы названий приложений"),
            ("/settings/app-icons", "Иконки приложений"),
            ("/settings/app-groups", "Группы приложений"),
            ("/settings/app-retention", "Retention по приложениям"),
            ("/settings/tag-aliases", "Алиасы тегов"),
            ("/api/tag-rules/stats.json", "Стата авто-тег правил"),
        ],
    },
    {
        "title_key": "settings_cat_notifications_title",
        "desc_key": "settings_cat_notifications_desc",
        "icon": "🔔",
        "pages": [
            ("/settings/integrations", "🔗 Интеграции — экспорт напоминаний в .ics + календари"),
            ("/settings/alice", "🗣️ Алиса (Яндекс) → Персона — голосовой навык с памятью"),
            ("/settings/smtp", "Email (SMTP)"),
            ("/settings/feed-tokens", "RSS токены доступа"),
            ("/feeds/all-opml", "OPML экспорт всех лент"),
            ("/webhooks", "Webhooks"),
        ],
    },
    {
        "title_key": "settings_cat_appearance_title",
        "desc_key": "settings_cat_appearance_desc",
        "icon": "🎨",
        "pages": [
            ("/settings/theme", "Тема (auto / light / dark)"),
            ("/settings/dashboard", "Дашборд (компоновка)"),
            ("/settings/dashboard-widgets", "Виджеты дашборда"),
            ("/settings/keyboard", "Горячие клавиши"),
            ("/help/shortcuts", "Справка по хоткеям"),
        ],
    },
    {
        "title_key": "settings_cat_security_title",
        "desc_key": "settings_cat_security_desc",
        "icon": "🔒",
        "pages": [
            ("/settings/privacy", "🔒 Приватность — что локально/в облако, экспорт, удалить всё"),
            ("/settings/api-tokens", "API токены"),
            ("/vault", "Vault (зашифрованные заметки)"),
            ("/settings/backup/manage", "Резервные копии БД"),
            ("/audit", "Audit log"),
            ("/admin/agents", "Remote agents (Mac)"),
            ("/settings/billing-admin", "💳 Биллинг — все подписки, ручной грант (owner)"),
        ],
    },
    {
        "title_key": "settings_cat_diagnostics_title",
        "desc_key": "settings_cat_diagnostics_desc",
        "icon": "📊",
        "pages": [
            ("/analytics", "📊 Аналитика — тренды активности и ИИ за 7/30/90 дней"),
            ("/day", "📅 День — обзор одного дня (скрины, звук, ИИ, спросить)"),
            ("/stats", "Общая статистика"),
            ("/stats/llm-cost", "Стоимость LLM"),
            ("/stats/quality-lab", "Качество захвата"),
            ("/activity", "Активность 365 дней"),
            ("/health", "Health-дашборд"),
            ("/settings/system-monitor", "🖥️ Монитор нагрузки ПК — CPU/RAM/диск/сеть в реальном времени"),
            ("/doctor", "Доктор (диагностика)"),
            ("/settings", "Все настройки одним списком (advanced)"),
        ],
    },
]


def _categories_json(lang: str | None = None) -> list[dict[str, object]]:
    """JS-friendly зеркало _CATEGORIES (кортежи pages → dict + keywords).

    Используется и для клиентского инстант-поиска (палитра в шапке), и для
    серверного ``/api/settings/search``. Один источник правды — _CATEGORIES.

    Заголовок/описание категории хранятся в _CATEGORIES как КЛЮЧИ переводов
    (``title_key`` / ``desc_key``) и резолвятся здесь через :func:`app.i18n.t`
    под активный язык интерфейса (``lang`` или ``get_ui_language()``), чтобы
    EN/DE-пользователь не видел русский текст.
    """
    effective_lang = lang if lang is not None else get_ui_language()
    out: list[dict[str, object]] = []
    for cat in _CATEGORIES:
        pages = []
        for href, label in cat["pages"]:  # type: ignore[union-attr]
            pages.append(
                {"href": href, "label": label, "keywords": _KEYWORDS.get(href, "")}
            )
        out.append(
            {
                "title": t(str(cat["title_key"]), effective_lang),
                "icon": cat["icon"],
                "description": t(str(cat["desc_key"]), effective_lang),
                "pages": pages,
            }
        )
    return out


def search_settings(query: str, limit: int = 30) -> list[dict[str, str]]:
    """Плоский поиск по всем страницам настроек (label/href/категория/синонимы)."""
    q = (query or "").strip().casefold()
    results: list[dict[str, str]] = []
    if not q:
        return results
    for cat in _categories_json():
        for p in cat["pages"]:  # type: ignore[index]
            hay = " ".join(
                [str(p["label"]), str(p["href"]), str(cat["title"]), str(p["keywords"])]
            ).casefold()
            if all(tok in hay for tok in q.split()):
                results.append(
                    {
                        "href": str(p["href"]),
                        "label": str(p["label"]),
                        "category": str(cat["title"]),
                        "icon": str(cat["icon"]),
                    }
                )
            if len(results) >= limit:
                return results
    return results


@router.get("/settings/hub", response_class=HTMLResponse)
async def settings_hub_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Render the category catalogue.

    Заголовки/описания категорий резолвятся под активный язык интерфейса:
    шаблон рисует карточки на Alpine.js из ``categories_json``, поэтому в нём
    уже лежат переведённые ``title`` / ``description`` (см. _categories_json).

    ``is_owner`` управляет видимостью блока экспорт/импорт профиля настроек:
    эти операции owner-only (см. settings_api.py), поэтому подписчику/триалу
    кнопки даже не рисуем.
    """
    resolved = _categories_json()
    owner = await is_owner(session["user_id"])
    return templates.TemplateResponse(
        request,
        "settings_hub.html",
        {
            "title": t("settings_hub_title"),
            "active_nav": "settings",
            "categories": resolved,
            "categories_json": resolved,
            "is_owner": owner,
        },
    )


@router.get("/api/settings/search", response_class=JSONResponse)
async def api_settings_search(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    q: str = "",
) -> JSONResponse:
    """Поиск по настройкам (для палитры/глобального поиска). Источник — _CATEGORIES."""
    return JSONResponse({"results": search_settings(q)})


__all__ = ["router", "search_settings"]
