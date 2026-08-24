"""Умная карточка «Интеллект» на Дашборде — 3 инсайта за 7 дней от ИИ.

Слайс E2 контракта «ИИ везде». Эндпоинт ``GET /api/dashboard/insights.json``
собирает те же 7-дневные статы, что и обычный дашборд (без нового SQL-хелпера —
переиспользуем :func:`app.web.routes.dashboard._collect_dashboard`), скармливает
их локальному ИИ (``make_client(kind="copilot")`` → провайдер ``worker``, модель
на ПК) и возвращает до трёх коротких буллетов-наблюдений.

Гейты и graceful-фоллбэки:

* Мастер-флаг «ИИ везде» ВЫКЛ → :func:`is_ai_everywhere` = False → 404 (карточки
  в шаблоне тоже нет, но эндпоинт закрываем на всякий случай).
* Требуется авторизация (:func:`current_user_required`).
* Результат КЭШИРУЕТСЯ на сутки в ``kv_settings`` (ключ + дата), иначе LLM
  считался бы на каждый заход на дашборд. Один расчёт в день.
* LLM недоступен (:class:`LLMNotConfigured`) или любой сбой → отдаём пустой
  список инсайтов с ``enabled: true`` (НЕ 500) — карточка сама покажет мягкую
  заглушку.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.routes.ai_everywhere_settings import is_ai_everywhere
from app.web.routes.dashboard import _collect_dashboard

log = get_logger("persona.dashboard.ai")

router = APIRouter(tags=["dashboard"])

#: Строка kv, где лежит суточный кэш (JSON: {"day": "...", "insights": [...]}).
#: Одна строка на всю установку — данные в Persona глобальные (не мультитенант).
_KV_CACHE = "dashboard_ai_insights_cache"

#: Жёсткий потолок на число буллетов — карточка компактная, три строки максимум.
_MAX_INSIGHTS = 3

#: Бюджет ответа: три коротких буллета укладываются с запасом.
_MAX_TOKENS = 320

_SYSTEM_PROMPT = (
    "Ты — «Интеллект», аналитик личной статистики внутри Persona. Тебе дают "
    "сводку активности пользователя за последние 7 дней (скриншоты, серия дней, "
    "топ-приложения). Верни РОВНО до трёх коротких наблюдений на русском языке: "
    "конкретных, полезных, без воды и без общих фраз. Каждое наблюдение — одна "
    "строка до 90 символов. Отвечай ТОЛЬКО валидным JSON-массивом строк, "
    'например ["наблюдение один", "наблюдение два"]. Никакого текста вне массива.'
)


def _today_iso() -> str:
    """Сегодняшняя дата ISO — ключ суточного кэша."""
    return date.today().isoformat()


def _build_stats_summary(payload: dict[str, Any]) -> str:
    """Сжать payload дашборда в компактную текстовую сводку для промпта.

    Берём только релевантные для инсайтов поля, чтобы не раздувать промпт и не
    гонять на слабый ПК-GPU лишний контекст.
    """
    today = payload.get("today") or {}
    streak = payload.get("streak") or {}
    top_apps = payload.get("top_apps") or []

    series = today.get("series") or []
    axis = today.get("axis") or []
    per_day = ", ".join(
        f"{day}: {count}" for day, count in zip(axis, series, strict=False)
    ) or "нет данных"
    apps = ", ".join(
        f"{a.get('app')} ({a.get('count')})" for a in top_apps
    ) or "нет данных"

    lines = [
        f"Кадров за 7 дней всего: {today.get('week_total', 0)}.",
        f"Сегодня кадров: {today.get('count', 0)}.",
        f"Кадров по дням: {per_day}.",
        f"Серия дней подряд: {streak.get('days', 0)} "
        f"(рекорд {streak.get('longest', 0)}).",
        f"Топ-приложения за 7 дней: {apps}.",
    ]
    return "\n".join(lines)


def _parse_insights(raw: str) -> list[str]:
    """Достать до трёх строк-инсайтов из ответа модели.

    Модель просят вернуть JSON-массив строк, но слабые локальные модели любят
    обернуть его в ```json ... ``` или добавить пояснение — аккуратно вычищаем
    и отдаём best-effort. Любой сбой → пустой список (карточка покажет заглушку).
    """
    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):  # снять markdown-обёртку ```json ... ```
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    # Вырезать сам массив, если модель добавила текст до/после.
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001 — модель могла вернуть не-JSON
        return []
    if not isinstance(parsed, list):
        return []
    insights: list[str] = []
    for item in parsed:
        line = str(item).strip()
        if line:
            insights.append(line)
        if len(insights) >= _MAX_INSIGHTS:
            break
    return insights


async def _load_cached(day: str) -> list[str] | None:
    """Вернуть закэшированные инсайты, если кэш за сегодняшний день."""
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, _KV_CACHE)
    except Exception:  # noqa: BLE001 — БД недоступна → считаем «кэша нет»
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001 — битый кэш
        return None
    if not isinstance(obj, dict) or obj.get("day") != day:
        return None
    cached = obj.get("insights")
    if isinstance(cached, list):
        return [str(x) for x in cached][:_MAX_INSIGHTS]
    return None


async def _store_cache(day: str, insights: list[str]) -> None:
    """Сохранить суточный кэш инсайтов (best-effort — сбой не критичен)."""
    try:
        async with get_connection() as conn:
            await set_kv(
                conn,
                _KV_CACHE,
                json.dumps({"day": day, "insights": insights}, ensure_ascii=False),
            )
    except Exception as exc:  # noqa: BLE001 — не даём сбою записи уронить ответ
        log.warning("dashboard.ai.cache_store_failed", error=str(exc))


async def _compute_insights(day: str) -> list[str]:
    """Посчитать инсайты через ИИ по 7-дневным статам и закэшировать на день."""
    payload = await _collect_dashboard()
    summary = _build_stats_summary(payload)

    client = make_client(kind="copilot")
    request = CompletionRequest(
        system=_SYSTEM_PROMPT,
        user="Сводка активности за 7 дней:\n" + summary,
        max_tokens=_MAX_TOKENS,
        temperature=0.5,
    )
    raw = await client.complete(request)
    insights = _parse_insights(raw)

    # Кэшируем даже пустой результат: без него на КАЖДЫЙ заход на дашборд мы
    # снова били бы по LLM. Пустой кэш живёт до конца суток, затем пересчёт.
    await _store_cache(day, insights)
    log.info("dashboard.ai.computed", day=day, count=len(insights))
    return insights


@router.get("/api/dashboard/insights.json", response_class=JSONResponse)
async def dashboard_insights(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Вернуть до трёх ИИ-инсайтов по 7-дневным статам (кэш раз в сутки).

    Гейт «ИИ везде» ВЫКЛ → 404. Ошибка LLM → пустой список (НЕ 500).
    """
    # Серверный гейт мастер-режима: при OFF благородный отказ 404, чтобы даже
    # прямой запрос к эндпоинту не считал LLM.
    if not await is_ai_everywhere():
        return JSONResponse(
            {"enabled": False, "insights": []},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    day = _today_iso()
    cached = await _load_cached(day)
    if cached is not None:
        return JSONResponse(
            {"enabled": True, "insights": cached, "cached": True},
            headers={"Cache-Control": "no-store"},
        )

    try:
        insights = await _compute_insights(day)
    except LLMNotConfigured as exc:
        # Локальный ИИ (ПК-воркер/провайдер) недоступен — мягкий отказ, не 500.
        log.info("dashboard.ai.llm_unavailable", error=str(exc))
        return JSONResponse(
            {"enabled": True, "insights": [], "reason": "llm_unavailable"},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:  # noqa: BLE001 — любой иной сбой не должен 500-ить
        log.warning("dashboard.ai.failed", error=str(exc))
        return JSONResponse(
            {"enabled": True, "insights": [], "reason": "error"},
            headers={"Cache-Control": "no-store"},
        )

    return JSONResponse(
        {"enabled": True, "insights": insights, "cached": False},
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
