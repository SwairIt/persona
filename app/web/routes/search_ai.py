"""ИИ-подсказки для поиска — «Возможно, вы искали…» + родственные термины.

Аддитивная приколюха поверх основного FTS-поиска (:mod:`app.web.routes.search`):
отдельный лёгкий JSON-эндпоинт ``GET /api/search/suggest.json?q=…``, который
спрашивает у копилот-LLM (провайдер ``worker`` — модель на ПК) исправление
опечатки и пару-тройку родственных терминов. Фронт дёргает его async и рисует
блок над результатами, НЕ блокируя горячий путь поиска.

Контракт «ИИ везде»:
* UI-видимость — Jinja-глобал ``get_ai_everywhere()`` в шаблоне.
* Серверный гейт — :func:`is_ai_everywhere`; при OFF отдаём 404 (эндпоинта как
  бы нет), а не считаем LLM впустую.
* LLM недоступен / кривой ответ → :class:`LLMNotConfigured` ловим и отдаём
  благородный ``{enabled: true, did_you_mean: null, related_terms: []}`` —
  НИКОГДА не 500, чтобы поисковая страница жила своей жизнью.

Кэш: короткий in-process TTL по нормализованному ``q`` — одинаковые наборы
букв в течение пары минут не гоняют модель повторно (набор в input по букве).
"""

from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.llm.client import CompletionRequest, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.web.routes.ai_everywhere_settings import is_ai_everywhere

log = get_logger("persona.search.suggest")

router = APIRouter(tags=["search"])

# Максимальная длина запроса, которую вообще отдаём модели. Длинные «запросы» —
# это, как правило, вставленный текст, а не поисковая фраза: подсказки для них
# бессмысленны и лишь жгут токены. Отсекаем тихо (пустой результат).
_MAX_Q_LEN = 120
# Сколько родственных терминов максимум показываем (модель может вернуть больше).
_MAX_RELATED = 6

# ---------------------------------------------------------------------------
# Короткий in-process кэш по нормализованному запросу.
# ---------------------------------------------------------------------------
_CACHE_TTL = 120.0  # секунд — набор в поле по букве не должен долбить модель
_CACHE_MAX = 256  # грубый предел, чтобы кэш не рос бесконечно
# key -> (expires_at, payload)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _norm_key(q: str) -> str:
    """Нормализуем запрос под ключ кэша: регистр + схлопнутые пробелы."""
    return re.sub(r"\s+", " ", q.strip().lower())


def _cache_get(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if not hit:
        return None
    expires_at, payload = hit
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    return payload


def _cache_put(key: str, payload: dict[str, Any]) -> None:
    # Ленивая уборка протухших записей + жёсткий предел размера, чтобы
    # долгоживущий процесс не тёк памятью на разнородных запросах.
    if len(_cache) >= _CACHE_MAX:
        now = time.monotonic()
        stale = [k for k, (exp, _) in _cache.items() if exp <= now]
        for k in stale:
            _cache.pop(k, None)
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
    _cache[key] = (time.monotonic() + _CACHE_TTL, payload)


# ---------------------------------------------------------------------------
# Промпт + парсинг ответа модели.
# ---------------------------------------------------------------------------
_SYSTEM = (
    "Ты — помощник поиска в приложении Persona (архив скриншотов с OCR-текстом). "
    "По запросу пользователя предложи: (1) исправленный запрос, если в нём "
    "явная опечатка (иначе null), и (2) 2-4 близких по смыслу поисковых "
    "термина, которые могут дать больше релевантных результатов. Отвечай СТРОГО "
    "одним JSON-объектом без markdown и пояснений, по схеме: "
    '{"did_you_mean": string|null, "related_terms": string[]}. '
    "Термины короткие (1-3 слова), на языке запроса, без дубликатов и без "
    "повтора самого запроса."
)


def _user_prompt(q: str) -> str:
    return f"Запрос пользователя: {q!r}\nВерни JSON."


def _extract_json_object(raw: str) -> dict[str, Any]:
    """Достаём объект ``{...}`` даже если модель обернула его в ```json ... ```."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("suggest: ответ модели не объект JSON")
    return parsed


def _clean_suggestions(parsed: dict[str, Any], q: str) -> dict[str, Any]:
    """Санитизируем сырой JSON модели в стабильный контракт ответа."""
    q_norm = _norm_key(q)

    dym_raw = parsed.get("did_you_mean")
    did_you_mean: str | None = None
    if isinstance(dym_raw, str):
        cand = dym_raw.strip()
        # Пустое, слишком длинное или совпадающее с запросом «исправление» —
        # это не исправление, отбрасываем.
        if cand and len(cand) <= _MAX_Q_LEN and _norm_key(cand) != q_norm:
            did_you_mean = cand

    related: list[str] = []
    seen: set[str] = {q_norm}
    raw_terms = parsed.get("related_terms")
    if isinstance(raw_terms, list):
        for item in raw_terms:
            if not isinstance(item, str):
                continue
            term = item.strip()
            key = _norm_key(term)
            if not term or key in seen or len(term) > _MAX_Q_LEN:
                continue
            seen.add(key)
            related.append(term)
            if len(related) >= _MAX_RELATED:
                break

    return {
        "enabled": True,
        "did_you_mean": did_you_mean,
        "related_terms": related,
    }


@router.get("/api/search/suggest.json", response_class=JSONResponse)
async def search_suggest(
    _session: Annotated[SessionRecord, Depends(current_user_required)],
    q: str = Query(default=""),
) -> JSONResponse:
    """ИИ-подсказки к поиску: исправление опечатки + родственные термины.

    Гейт «ИИ везде» OFF → 404 (эндпоинта как бы нет). Пустой/слишком длинный
    запрос → пустой (но валидный) ответ. LLM недоступен → благородный пустой
    результат, НЕ 500.
    """
    # Серверный гейт мастер-режима: при OFF ведём себя так, будто маршрута нет.
    if not await is_ai_everywhere():
        raise HTTPException(status_code=404, detail="Not Found")

    query = q.strip()
    empty = {"enabled": True, "did_you_mean": None, "related_terms": []}
    if not query or len(query) > _MAX_Q_LEN:
        return JSONResponse(empty, headers={"Cache-Control": "no-store"})

    key = _norm_key(query)
    cached = _cache_get(key)
    if cached is not None:
        return JSONResponse(cached, headers={"Cache-Control": "no-store"})

    try:
        client = make_client(kind="copilot")
        req = CompletionRequest(
            system=_SYSTEM,
            user=_user_prompt(query),
            max_tokens=180,
            temperature=0.2,
        )
        raw = (await client.complete(req)).strip()
        payload = _clean_suggestions(_extract_json_object(raw), query)
    except LLMNotConfigured as exc:
        # Модель на ПК офлайн / провайдер не настроен — это НЕ ошибка страницы.
        log.info("search.suggest.llm_unavailable", error=str(exc))
        return JSONResponse(empty, headers={"Cache-Control": "no-store"})
    except Exception as exc:  # noqa: BLE001 — кривой JSON/сеть → тихий пустой ответ
        log.warning("search.suggest.failed", error=str(exc))
        return JSONResponse(empty, headers={"Cache-Control": "no-store"})

    # Кэшируем только осмысленный результат (что-то нашлось), чтобы пустышки
    # не залипали и следующий заход мог повезти на живой модели.
    if payload.get("did_you_mean") or payload.get("related_terms"):
        _cache_put(key, payload)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


__all__ = ["router"]
