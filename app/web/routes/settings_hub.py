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

from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.templates_engine import templates

router = APIRouter(tags=["settings-hub"])


_CATEGORIES: Final[list[dict[str, object]]] = [
    {
        "title": "Захват",
        "icon": "🎥",
        "description": "Скриншоты, аудио, мик, расписание, throttle бюджета.",
        "pages": [
            ("/settings/capture", "Основное (вкл/выкл, частота, расписание)"),
            ("/settings/audio", "Расширенные параметры аудио (codec, VAD)"),
            ("/settings/blocklist", "Регекс-блоклист окон/приложений"),
            ("/settings/app-capture-skip", "Список приложений-исключений"),
            ("/settings/app-overrides", "Per-app частота захвата"),
            ("/settings/quiet-hours", "Quiet hours (часы без захвата)"),
        ],
    },
    {
        "title": "OCR и распознавание",
        "icon": "🔍",
        "description": "Tesseract, языки, фразы для тегов, редактирование.",
        "pages": [
            ("/settings/ocr-languages", "Языки OCR"),
            ("/settings/ocr-skip", "Skip-list для OCR"),
            ("/settings/phrase-tags", "Фразовые правила → теги"),
            ("/settings/redaction", "Регексы для скрытия секретов в OCR"),
            ("/ocr-admin", "OCR-админка (статус, perepriem)"),
        ],
    },
    {
        "title": "Память и сводки",
        "icon": "🧠",
        "description": "LLM, дайджесты, обогащение карточек, перевод.",
        "pages": [
            ("/settings/llm", "Провайдер + ключ LLM"),
            ("/settings/digest-prompt", "Промпт для дайджестов"),
            ("/memory", "Tier 1+5: hourly cards + daily pins"),
            ("/memory/weeks", "Tier 2: weekly cards"),
        ],
    },
    {
        "title": "AI, чат и инструменты",
        "icon": "🤖",
        "description": "Чат с памятью, MCP-инструменты, workspace, датасет для своей модели.",
        "pages": [
            ("/chat", "Чат с памятью (беседы, модель-пикер, vision)"),
            ("/settings/profile", "Профиль — что ИИ знает обо мне"),
            ("/settings/system-prompt", "Системный промпт чата (пресеты + редактор)"),
            ("/settings/mac-fs", "AI и файлы — пишет прямо на Mac (allowlist) + выбор устройства"),
            ("/ask", "Спросить — разовый вопрос по истории"),
            ("/admin/mcp", "MCP-серверы и встроенные инструменты"),
            ("/admin/dataset", "Датасет Q&A для fine-tune PersonaAI"),
            ("/settings/llm", "Провайдер + ключ LLM"),
        ],
    },
    {
        "title": "Устройства и синхронизация",
        "icon": "📱",
        "description": "Mac/iPhone агенты, выбор куда писать код, sync, хранилище.",
        "pages": [
            ("/devices", "Устройства (пауза, интервал, ★ код-таргет)"),
            ("/welcome/install/mac", "Установить / обновить Mac-агент"),
            ("/welcome", "Онбординг — подключить новое устройство"),
            ("/sync", "Синхронизация — статус и watermark'и"),
            ("/storage", "Хранилище — квоты, retention, очистка"),
            ("/admin/agents", "Remote agents — токены ingest"),
        ],
    },
    {
        "title": "Приложения и теги",
        "icon": "🏷",
        "description": "Алиасы, иконки, группы, retention per-app.",
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
        "title": "Уведомления и интеграции",
        "icon": "🔔",
        "description": "SMTP, RSS-токены, webhooks.",
        "pages": [
            ("/settings/smtp", "Email (SMTP)"),
            ("/settings/feed-tokens", "RSS токены доступа"),
            ("/feeds/all-opml", "OPML экспорт всех лент"),
            ("/webhooks", "Webhooks"),
        ],
    },
    {
        "title": "Внешний вид",
        "icon": "🎨",
        "description": "Тема, плотность, доступность, дашборд.",
        "pages": [
            ("/settings/theme", "Тема (auto / light / dark)"),
            ("/settings/dashboard", "Дашборд (компоновка)"),
            ("/settings/dashboard-widgets", "Виджеты дашборда"),
            ("/settings/keyboard", "Горячие клавиши"),
            ("/help/shortcuts", "Справка по хоткеям"),
        ],
    },
    {
        "title": "Безопасность и обслуживание",
        "icon": "🔒",
        "description": "Токены API, vault, бэкапы, аудит, hibrnation.",
        "pages": [
            ("/settings/api-tokens", "API токены"),
            ("/vault", "Vault (зашифрованные заметки)"),
            ("/settings/backup/manage", "Резервные копии БД"),
            ("/audit", "Audit log"),
            ("/admin/agents", "Remote agents (Mac)"),
        ],
    },
    {
        "title": "Диагностика",
        "icon": "📊",
        "description": "Статистика, бюджет, lab-замеры, фокус.",
        "pages": [
            ("/stats", "Общая статистика"),
            ("/stats/llm-cost", "Стоимость LLM"),
            ("/stats/quality-lab", "Качество захвата"),
            ("/activity", "Активность 365 дней"),
            ("/health", "Health-дашборд"),
            ("/doctor", "Доктор (диагностика)"),
            ("/settings", "Все настройки одним списком (advanced)"),
        ],
    },
]


@router.get("/settings/hub", response_class=HTMLResponse)
async def settings_hub_page(request: Request) -> HTMLResponse:
    """Render the category catalogue."""
    return templates.TemplateResponse(
        request,
        "settings_hub.html",
        {
            "title": "Настройки — категории",
            "active_nav": "settings",
            "categories": _CATEGORIES,
        },
    )


__all__ = ["router"]
