"""Слайс D1 — «найди настройку через ИИ».

Обычная палитра настроек (Ctrl+Shift+P → ``/api/settings/search``) ищет по
ключевым словам: label/href/категория/синонимы (см. ``search_settings`` в
``settings_hub.py``). Этого хватает, когда пользователь помнит термин
(«тема», «бэкап»), но проваливается на естественном намерении вроде
«хочу чтобы сайт не тормозил» или «сделай чтобы меньше следили за мной».

Этот эндпоинт добавляет ИИ-слой ПОВЕРХ keyword-поиска:

1. Сначала честный keyword-поиск ``search_settings(intent)`` — без LLM,
   мгновенно и бесплатно. Если он уже дал приличный результат — отдаём его
   (``ai_used=false``), LLM не трогаем.
2. Если пусто/слабо — просим копилот (модель на ПК через worker-провайдер)
   переформулировать намерение в 3-5 ключевых слов и повторяем keyword-поиск
   по ним. Возвращаем объединённый список (``ai_used=true``).

Гейт: работает только при мастер-режиме «ИИ везде» (``is_ai_everywhere``).
OFF → 404 (фича не существует для внешнего мира, палитра тихо фоллбэчит на
обычный поиск). LLM недоступен (``LLMNotConfigured`` / любой сбой) →
graceful: отдаём хотя бы keyword-результат, не 500.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.web.routes.ai_everywhere_settings import is_ai_everywhere
from app.web.routes.settings_hub import search_settings

router = APIRouter(tags=["settings-ai-search"])

log = get_logger("persona.settings_ai_search")


async def _owner_id(session: SessionRecord) -> int:
    """Settings — включая ИИ-эндпоинты — только для владельца (owner-gate).

    Остальные аутентифицированные аккаунты в Persona сандбоксятся от
    приватной поверхности (см. ``app/auth/owner.py``); мастер-тумблер
    «ИИ везде» этого не покрывает, поэтому владельца проверяем отдельно.
    """
    user_id = int(session["user_id"])
    if not await is_owner(user_id):
        raise HTTPException(status_code=403, detail="Только владелец")
    return user_id

#: Ниже этого числа keyword-совпадений намерение считаем «слабым» и зовём ИИ.
#: 1-2 совпадения по длинной естественной фразе часто мусорные, поэтому даём
#: модели шанс переформулировать. Порог намеренно низкий: keyword-поиск и так
#: приоритетен, ИИ лишь дополняет.
_WEAK_THRESHOLD = 2

#: Разумный потолок символов намерения — длинную простыню не шлём в LLM.
_MAX_INTENT_LEN = 400


class _AiSearchIn(BaseModel):
    """Тело POST /api/settings/ai-search."""

    intent: str = ""


def _score_for(rank: int) -> float:
    """Псевдо-релевантность: чем выше в выдаче, тем больше. Для сортировки/UI.

    ``search_settings`` уже возвращает результаты в порядке приоритета
    (по категориям сверху вниз), поэтому просто конвертируем позицию в
    убывающий скор 1.0…~0.
    """
    return round(1.0 / (1.0 + rank), 4)


def _as_results(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    """Плоские строки search_settings → контракт {href,label,score}.

    ``category``/``icon`` пробрасываем тоже — палитра умеет их показывать,
    но контракт слайса требует минимум href/label/score, так что они опциональны.
    """
    out: list[dict[str, object]] = []
    for rank, row in enumerate(rows):
        out.append(
            {
                "href": row.get("href", ""),
                "label": row.get("label", ""),
                "score": _score_for(rank),
                "category": row.get("category", ""),
                "icon": row.get("icon", ""),
            }
        )
    return out


def _merge_results(
    primary: list[dict[str, object]], extra: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Склеить keyword- и ИИ-результаты, убрав дубли по href (primary важнее)."""
    seen = {str(r["href"]) for r in primary if r.get("href")}
    merged = list(primary)
    for r in extra:
        href = str(r.get("href", ""))
        if href and href not in seen:
            seen.add(href)
            merged.append(r)
    return merged


def _extract_keywords(raw: str) -> str:
    """Из ответа LLM вытащить 3-5 ключевых слов, отбросив пояснения/мусор.

    Модель просят вернуть слова через запятую; но локальная 3-7B может
    добавить преамбулу/markdown. Берём последнюю непустую строку, режем по
    запятым/переносам, чистим кавычки и служебные символы, ограничиваем 5.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # Снять возможную ``` обёртку.
    text = re.sub(r"```[a-zA-Z0-9]*", " ", text).replace("```", " ")
    # Взять последнюю содержательную строку (модель часто «рассуждает» выше).
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidate = lines[-1] if lines else text
    # Разбить по запятым/точкам-с-запятой/переносам.
    parts = re.split(r"[,;\n]+", candidate)
    words: list[str] = []
    for p in parts:
        w = p.strip().strip("\"'`.-•*").strip()
        # Убрать нумерацию «1) », «2. » и т.п.
        w = re.sub(r"^\d+[.)]\s*", "", w).strip()
        if w and len(w) <= 40:
            words.append(w)
        if len(words) >= 5:
            break
    return " ".join(words)


async def _ai_keywords(intent: str) -> str:
    """Переформулировать намерение в ключевые слова через копилот (worker).

    Возвращает строку ключевых слов или "" при любой недоступности LLM —
    вызывающий код обязан работать и с пустой строкой (graceful).
    """
    system = (
        "Ты — поисковый помощник по настройкам приложения Persona. "
        "Пользователь описывает СВОИМИ словами, что хочет настроить или "
        "сделать. Твоя задача — выдать 3-5 коротких ключевых слов (термины "
        "настроек, синонимы) на русском и английском, по которым это можно "
        "найти. Отвечай ТОЛЬКО ключевыми словами через запятую, без пояснений, "
        "без markdown, без нумерации."
    )
    user = f"Намерение пользователя: {intent}\nКлючевые слова:"
    client = make_client(kind="copilot")
    raw = await client.complete(
        CompletionRequest(system=system, user=user, max_tokens=64, temperature=0.2)
    )
    return _extract_keywords(raw)


@router.post("/api/settings/ai-search", response_class=JSONResponse)
async def api_settings_ai_search(
    payload: _AiSearchIn,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """ИИ-поиск по настройкам: keyword → (если слабо) LLM-переформулировка.

    Гейт «ИИ везде»: OFF → 404 (палитра фоллбэчит на обычный /search).
    Контракт ответа: ``{results:[{href,label,score,category,icon}], ai_used:bool}``.
    """
    await _owner_id(session)
    if not await is_ai_everywhere():
        # Благородный отказ: для внешнего мира фичи как будто нет.
        raise HTTPException(status_code=404, detail="AI search disabled")

    intent = (payload.intent or "").strip()[:_MAX_INTENT_LEN]
    if not intent:
        return JSONResponse({"results": [], "ai_used": False})

    # Шаг 1 — честный keyword-поиск (мгновенно, без LLM).
    keyword_rows = _as_results(search_settings(intent))
    if len(keyword_rows) > _WEAK_THRESHOLD:
        # Уже нашли достаточно — ИИ не нужен.
        return JSONResponse({"results": keyword_rows, "ai_used": False})

    # Шаг 2 — намерение слабо матчится: просим ИИ переформулировать.
    ai_used = False
    try:
        kw = await _ai_keywords(intent)
        if kw:
            ai_used = True
            ai_rows = _as_results(search_settings(kw))
            merged = _merge_results(keyword_rows, ai_rows)
            log.info(
                "settings_ai_search.ai",
                intent_len=len(intent),
                keyword_hits=len(keyword_rows),
                ai_hits=len(ai_rows),
                merged=len(merged),
            )
            return JSONResponse({"results": merged, "ai_used": ai_used})
    except LLMNotConfigured as exc:
        # LLM не настроен/офлайн — graceful: отдаём keyword-результат.
        log.info("settings_ai_search.llm_unavailable", error=str(exc))
    except Exception as exc:  # noqa: BLE001 — любой сбой ИИ не должен ронять поиск
        log.warning("settings_ai_search.ai_failed", error=str(exc))

    # Фоллбэк: хотя бы keyword-результат (возможно пустой), НЕ 500.
    return JSONResponse({"results": keyword_rows, "ai_used": ai_used})


__all__ = ["router"]
