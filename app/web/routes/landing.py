"""Public-facing landing page — the product's main page.

Routes:
    * ``GET /``        — home. Logged-in → /now (cabinet). Logged-out → landing.
    * ``GET /landing`` — always renders the landing (even when signed in it
      shows a "you're already signed in as X — continue?" state, per the
      product spec), so a shared marketing link works for everyone.

The page is a standalone marketing template (own design, does NOT extend
base.html) with a Three.js hero, scroll-driven animations and SEO meta.
It is in the auth-gate public allow-list so search engines and logged-out
visitors can see it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import __version__, blog
from app.auth import current_user_optional
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["landing"])
log = get_logger("persona.landing")


def _render(
    request: Request,
    session: SessionRecord | None,
    template: str = "landing.html",
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        template,
        {
            "title": "Persona",
            "active_nav": "",
            "app_version": __version__,
            "session": session,
            "posts": blog.list_posts()[:3],
        },
    )


@router.get("/", response_class=HTMLResponse, response_model=None)
async def home(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse | RedirectResponse:
    """Home: signed-in users go to the cabinet, everyone else sees the landing."""
    if session is not None:
        return RedirectResponse(url="/now", status_code=303)
    return _render(request, None)


@router.get("/landing", response_class=HTMLResponse, response_model=None)
async def landing_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    """Always render the landing; auth-aware CTA (continue-as-X when signed in)."""
    return _render(request, session)


@router.get("/features", response_class=HTMLResponse, response_model=None)
async def features_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    """Публичная страница возможностей (12 преимуществ, local-first акцент)."""
    return templates.TemplateResponse(
        request,
        "features.html",
        {"title": "Возможности Persona", "app_version": __version__, "session": session},
    )


# Детальные сравнения (BUILD_PLAN C3). Факты — из ресёрча competitors_brief.md.
_COMPARE: dict[str, dict] = {
    "rewind": {
        "rival": "Rewind / Limitless",
        "title": "Persona vs Rewind / Limitless",
        "lead": "Эталон local-first памяти ушёл в облако и был куплен Meta. Persona — открытая, её нельзя выключить.",
        "hook": "Декабрь 2025: Rewind отключён, Limitless куплен Meta (запись на Mac выключена с 19.12.2025), "
                "сервис отрезан в EU/UK. Существующим — поддержка ~1 год, дальше технология уходит в носимые устройства Meta.",
        "rows": [
            ("Приватность / локальность", "Local-first, опц. полностью офлайн (Ollama)", "Стартовал local-first → ушёл в облако (Pendant), теперь у Meta"),
            ("Статус продукта", "Активен, открыт, развивается", "Свёрнут / поглощён; Mac-запись отключена 19.12.25"),
            ("Цена", "Открыто, без вечной облачной подписки", "$19–50/мес + Pendant $99–399"),
            ("Платформы", "Mac + Windows", "Только macOS"),
            ("Чат с памятью", "Кросс-чат recall + bi-temporal факты (история, откат)", "Ask AI по истории"),
            ("Граф памяти", "Да", "Нет"),
            ("Своя дообученная модель", "Да — «вторая копия» (QLoRA, локально)", "Нет — проприетарный/облачный LLM"),
            ("Открытость", "Да, без vendor lock-in", "Нет, проприетарно"),
            ("Экспорт данных", "Без потерь, в любой момент", "Есть (особенно «окно перед удалением» при закрытии в EU/UK)"),
        ],
        "verdict": "После поглощения Meta у рынка образовался вакуум доверия. Persona закрывает его архитектурно: "
                   "нет облачного бэкенда, который можно «переключить», и кода, который нельзя проверить.",
    },
    "recall": {
        "rival": "Microsoft Recall",
        "title": "Persona vs Microsoft Recall",
        "lead": "Память без нового ноутбука и без закрытого кода.",
        "hook": "Microsoft Recall требует Copilot+ PC (NPU ≥40 TOPS), работает только по экрану (без звука), "
                "код закрыт, а в 2025–2026 ловил эксплойты и утечки чувствительных данных.",
        "rows": [
            ("Требования к железу", "Обычный ПК", "Только Copilot+ PC (NPU ≥40 TOPS)"),
            ("Открытость", "Да — аудируемо", "Нет — закрытый код Windows"),
            ("Приватность", "Проверяемая (всё локально, опц. офлайн)", "Локально, но закрыто; были утечки/эксплойты"),
            ("Запись звука", "Да + транскрипция", "Нет — только экран"),
            ("Чат с памятью", "Да, диалоговый ассистент", "Поиск + Click-to-Do, не диалог"),
            ("Своя модель", "Да — «вторая копия»", "Нет — закрытая on-device, привязана к NPU"),
            ("Кроссплатформа", "Mac + Windows", "Только Windows 11 Copilot+"),
            ("Граф памяти", "Да", "Нет"),
            ("Экспорт данных", "Без потерь", "Ограничен"),
        ],
        "verdict": "Recall заставляет купить новый ноутбук и довериться закрытому коду. Persona работает на твоём "
                   "ПК, открыта и проверяема, пишет и экран, и звук, и умеет диалоговую память.",
    },
}


@router.get("/compare/{slug}", response_class=HTMLResponse, response_model=None)
async def compare_page(
    request: Request,
    slug: str,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    """Публичное детальное сравнение Persona vs конкурент."""
    data = _COMPARE.get(slug.lower())
    if data is None:
        from fastapi import HTTPException  # noqa: PLC0415

        raise HTTPException(status_code=404, detail="unknown comparison")
    return templates.TemplateResponse(
        request,
        "compare.html",
        {"title": data["title"], "app_version": __version__, "session": session,
         "c": data, "slug": slug.lower()},
    )


@router.get("/pricing", response_class=HTMLResponse, response_model=None)
async def pricing_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    """Публичная страница цен — честно: локально бесплатно, облако по своему ключу."""
    return templates.TemplateResponse(
        request,
        "pricing.html",
        {"title": "Цена Persona", "app_version": __version__, "session": session},
    )


# Юридические/безопасность страницы (BUILD_PLAN C5). Честно, без выдуманных
# сертификаций/гарантий — описываем реальную local-first модель данных.
_LEGAL_UPDATED = "17 июня 2026"
_LEGAL: dict[str, dict] = {
    "security": {
        "path": "/security", "title": "Безопасность",
        "lead": "Persona — local-first: безопасность строится на том, что данные не покидают твоё устройство, а не на доверии к чужому облаку.",
        "sections": [
            ("Модель данных", ["Захват экрана, OCR, аудио, индекс и память хранятся локально на твоём устройстве.",
                               "В облако ничего не уходит без твоего явного включения. С локальной моделью (Ollama) Persona работает полностью офлайн."]),
            ("Что помогает защититься", ["Vault для секретов (шифрование), исключения приложений/окон из захвата, пауза и удаление в любой момент.",
                                          "Redaction — маскирование чувствительного в OCR. Air-gapped режим (локальная LLM) — запросы не уходят наружу."]),
            ("Честно о рисках", ["Тотальный локальный лог того, что ты видишь — потенциальная цель для малвари или форензики на твоём же устройстве. Мы не отрицаем риск — мы даём контроль: шифрование, исключения, пауза, удаление, локальная модель.",
                                  "Защищай само устройство (шифрование диска, пароль/биометрия) — это первая линия обороны."]),
            ("Раскрытие уязвимостей", ["Нашёл проблему безопасности — сообщи через GitHub-репозиторий проекта. Мы ценим ответственное раскрытие."]),
            ("Чего мы НЕ заявляем", ["У Persona нет формальных сертификаций (HIPAA, SOC 2 и т.п.) и мы их не имитируем. Local-first снижает облачные риски, но не заменяет твою собственную операционную безопасность."]),
        ],
    },
    "privacy-policy": {
        "path": "/privacy-policy", "title": "Политика приватности",
        "lead": "Коротко: твои данные — твои. Persona хранит их у тебя и не торгует ими.",
        "sections": [
            ("Что собирается и где хранится", ["То, что ты захватываешь (скриншоты, OCR-текст, аудио, чаты, память), хранится локально на твоём устройстве/в твоей инсталляции. Это не наш облачный аккаунт."]),
            ("Что уходит наружу", ["По умолчанию — ничего. Если ты подключаешь облачную LLM по своему API-ключу, наружу уходят только тексты запросов к этому провайдеру (по его политике), но не архив захвата.",
                                    "Хочешь полностью офлайн — используй локальную модель (Ollama)."]),
            ("Телеметрия", ["Persona не строится вокруг слежки за пользователем. Никакой продажи данных третьим лицам."]),
            ("Твои права", ["Экспорт всех данных без потерь и удаление — в любой момент, через настройки приватности. Никакого «окна на экспорт перед закрытием», как у свернувшихся облачных сервисов."]),
            ("Самостоятельный хостинг", ["Если ты разворачиваешь Persona у себя — обработка данных целиком под твоим контролем и твоей юрисдикцией."]),
        ],
    },
    "terms": {
        "path": "/terms", "title": "Условия использования",
        "lead": "Простыми словами, без мелкого шрифта против тебя.",
        "sections": [
            ("Открытость", ["Persona распространяется как открытый проект. Ты можешь использовать, изучать и разворачивать её у себя."]),
            ("Ответственность пользователя", ["Ты записываешь СВОЙ экран и звук на СВОём устройстве. Если в записи попадают другие люди — соблюдение согласия и местных законов о записи лежит на тебе. Persona даёт инструменты (пауза, исключения, redaction), чтобы это контролировать."]),
            ("Без гарантий", ["Программа предоставляется «как есть», без гарантий пригодности для конкретной цели. Используй на свой риск; делай резервные копии важного."]),
            ("Изменения", ["Условия могут уточняться; актуальная версия — на этой странице с датой обновления."]),
        ],
    },
}


def _legal_route(slug: str):
    async def _page(
        request: Request,
        session: Annotated[SessionRecord | None, Depends(current_user_optional)],
    ) -> HTMLResponse:
        data = _LEGAL[slug]
        return templates.TemplateResponse(
            request, "legal.html",
            {"title": data["title"], "app_version": __version__, "session": session,
             "doc": data, "updated": _LEGAL_UPDATED},
        )
    return _page


for _slug, _data in _LEGAL.items():
    router.add_api_route(_data["path"], _legal_route(_slug), methods=["GET"],
                         response_class=HTMLResponse, response_model=None)


# Roadmap + changelog (BUILD_PLAN C6). Честно: готово/в работе/планируется; без обещаний дат.
_INFO: dict[str, dict] = {
    "roadmap": {
        "path": "/roadmap", "title": "Дорожная карта",
        "lead": "Что уже работает и куда движемся. Без обещаний дат — local-first продукт развивается по готовности.",
        "sections": [
            ("✅ Готово", [
                "Захват экрана + звук (агенты Mac и Windows), OCR, дедуп",
                "Часовая и дневная память, граф памяти",
                "Чат с памятью: кросс-чат recall + bi-temporal факты (история, откат)",
                "Единая страница дня + «спросить про этот день»",
                "Аналитика за период (7/30/90)",
                "Приватность: дашборд, экспорт памяти/БД, локальная модель (Ollama)",
                "Проактивность: брифинг-карточки + тихие часы, NL-напоминания",
                "Интеграции: экспорт в .ics, импорт Markdown-заметок",
                "Своя «вторая копия»: сбор датасета + QLoRA-пайплайн (Qwen3-Thinking)",
                "Голос: серверный каркас конфига движков",
            ]),
            ("🛠 В работе", [
                "Маркетинговый сайт и блог (контент, сравнения, гайды)",
                "vec0-ускорение поиска по скринам (тихий fallback без sqlite-vec)",
            ]),
            ("🗺 Планируется", [
                "Голосовые движки на устройстве (faster-whisper, Silero VAD, Piper/Kokoro/Silero TTS, barge-in)",
                "Активация векторного recall (sqlite-vec + локальные эмбеддинги)",
                "Больше локальных интеграций (календарь, заметки) — opt-in",
            ]),
        ],
    },
    "changelog": {
        "path": "/changelog", "title": "История изменений",
        "lead": "Ключевые улучшения последних релизов линейки 2.20.x.",
        "sections": [
            ("Память и доверие", [
                "Bi-temporal факты (soft-invalidate) + mem0-реконсиляция, GBNF-извлечение",
                "Дашборд приватности, экспорт памяти и снимок БД, бейдж провайдера 🔒/☁ в чате",
                "Spotlighting (recall/экран как ДАННЫЕ) + ре-инъекция персоны в длинных беседах",
            ]),
            ("Проактивность и интеграции", [
                "Брифинг-карточки с обратной связью + тихие часы",
                "NL-планирование задач («напомни завтра …»)",
                "Экспорт напоминаний в .ics, импорт Markdown в память",
            ]),
            ("Кабинет: день и аналитика", [
                "Единая страница дня /day/{date}: KPI, скрины по часам, «спросить про день»",
                "Сквозная навигация в день: граф, память, календарь, тепловая карта",
                "Страница аналитики /analytics (тренды, топ-приложения, использование ИИ)",
            ]),
            ("Сайт", [
                "Обновлённое сравнение с конкурентами, /features, /compare/*, /pricing, /security, /privacy-policy, /terms",
            ]),
        ],
    },
}


def _info_route(slug: str):
    async def _page(
        request: Request,
        session: Annotated[SessionRecord | None, Depends(current_user_optional)],
    ) -> HTMLResponse:
        data = _INFO[slug]
        return templates.TemplateResponse(
            request, "infopage.html",
            {"title": data["title"], "app_version": __version__, "session": session, "doc": data},
        )
    return _page


for _islug, _idata in _INFO.items():
    router.add_api_route(_idata["path"], _info_route(_islug), methods=["GET"],
                         response_class=HTMLResponse, response_model=None)


@router.get("/landing/v2", response_class=HTMLResponse, response_model=None)
async def landing_page_v2(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    """Alternate "starlit violet cosmos" landing with a 3D black-hole hero.

    Same content + auth-aware CTAs as ``/landing``, different design. Covered
    by the ``/landing`` public allow-list prefix, so logged-out visitors reach
    it. Kept ``noindex`` (in the template) to avoid duplicate-content with v1.
    """
    return _render(request, session, template="landing_v2.html")
