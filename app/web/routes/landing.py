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
