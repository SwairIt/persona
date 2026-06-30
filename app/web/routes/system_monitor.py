"""Монитор нагрузки ПК — CPU/RAM/диск/сеть/процессы в реальном времени.

Owner-only страница (`/settings/system-monitor`) + JSON-эндпоинт для
поллинга каждые 3-5 секунд (`/api/system-monitor.json`). Сами метрики
собирает соседний слайс — :mod:`app.system_metrics` (S1): дёргаем
``collect_system_metrics()`` (текущий снимок) и ``get_history()`` (ряд для
спарклайнов).

Гейт владельца — копия паттерна из :func:`app.web.routes.billing.billing_admin`:
``Depends(current_user_required)`` + ``await is_owner(uid)`` → 403, чтобы
покупатель/гость не видел железо хоста.

Best-effort: если psutil не установлен или сбор метрик упал — отдаём пустой
снимок вместо 500, чтобы страница/поллинг не ронял приложение.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger

# Зависимость от слайса S1. Сбор метрик — best-effort, поэтому при любой
# проблеме внутри возвращаем безопасный пустой снимок (см. _safe_snapshot).
from app.system_metrics import collect_system_metrics, get_history
from app.web.templates_engine import templates

router = APIRouter(tags=["system-monitor"])
log = get_logger("persona.system_monitor")


async def _require_owner(session: SessionRecord) -> int:
    """Пускаем только владельца хоста; иначе 403 (как billing_admin)."""
    uid = int(session["user_id"])
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="Только владелец")
    return uid


def _safe_snapshot() -> dict[str, object]:
    """Текущий снимок метрик с тихим fallback на пустой dict.

    Внешний сбой (нет psutil, отказ драйвера сенсоров и т.п.) НЕ должен
    ронять страницу — отдаём пустой снимок, UI деградирует мягко.
    """
    try:
        snap = collect_system_metrics()
        return dict(snap) if snap else {}
    except Exception as exc:  # noqa: BLE001 — метрики опциональны, не валим страницу
        log.warning("system_monitor.collect_failed", error=str(exc))
        return {}


def _safe_history() -> list[object]:
    """Исторический ряд для спарклайнов с тихим fallback на пустой список."""
    try:
        hist = get_history()
        return list(hist) if hist else []
    except Exception as exc:  # noqa: BLE001 — история опциональна
        log.warning("system_monitor.history_failed", error=str(exc))
        return []


@router.get("/settings/system-monitor", response_class=HTMLResponse, response_model=None)
async def system_monitor_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Страница монитора нагрузки. Только владелец. Рендер начального снимка."""
    await _require_owner(session)
    return templates.TemplateResponse(
        request,
        "system_monitor.html",
        {
            "title": "Монитор нагрузки ПК",
            "app_version": __version__,
            "active_nav": "settings",
            "is_owner": True,
            "session": session,
            "snapshot": _safe_snapshot(),
            "history": _safe_history(),
        },
    )


@router.get("/api/system-monitor.json", response_model=None)
async def system_monitor_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Снимок + история для JS-поллинга (каждые 3-5с). Только владелец."""
    await _require_owner(session)
    return JSONResponse(
        {
            "snapshot": _safe_snapshot(),
            "history": _safe_history(),
        }
    )


__all__ = ["router"]
