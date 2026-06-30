"""Кабинет биллинга /billing — план, лицензионный ключ, апгрейд.

Сюда попадают НЕ-владельцы после входа (вместо тупиковой /pending): покупатель
видит свой план/триал и лицензионный ключ, который вставляет в свою Persona.
Приложение (данные владельца) им недоступно — гейт пускает только /billing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import __version__
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.billing import config as billing_config
from app.billing import repo
from app.billing import service
from app.billing.licensing import subscription_active
from app.billing.plans import PRO_MONTHLY, PRO_YEARLY
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web import rate_limit
from app.web.templates_engine import templates

router = APIRouter(tags=["billing"])
log = get_logger("persona.billing.routes")


async def _render_portal(
    request: Request,
    session: SessionRecord,
    *,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    uid = int(session["user_id"])
    return templates.TemplateResponse(
        request,
        "billing.html",
        {
            "title": "Подписка",
            "app_version": __version__,
            "session": session,
            "owner": await is_owner(uid),
            "summary": await service.summary(uid),
            "plans": [PRO_MONTHLY, PRO_YEARLY],
            "configured": billing_config.is_configured(),
            "email": session.get("email"),
            "notice": notice,
        },
        status_code=status_code,
    )


@router.get("/billing", response_class=HTMLResponse, response_model=None)
async def billing_portal(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Кабинет подписки: план, триал-обратный-отсчёт, лицензионный ключ, апгрейд."""
    return await _render_portal(request, session)


@router.post("/billing/checkout", response_class=HTMLResponse, response_model=None)
async def billing_checkout(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    plan: Annotated[str, Form()],
) -> Response:
    """Старт оплаты. Пока ЮKassa не настроена владельцем — честно сообщаем (триал
    работает). Когда настроена — создаём платёж и редиректим на оплату."""
    if not billing_config.is_configured():
        return await _render_portal(
            request, session,
            notice="Приём оплаты скоро подключится. Твой триал уже активен — ключ выше.",
        )
    base = str(request.base_url).rstrip("/")
    try:
        url = await service.start_checkout(int(session["user_id"]), plan, f"{base}/billing")
    except Exception as exc:  # noqa: BLE001 — любая ошибка платёжки → дружелюбно
        log.warning("billing.checkout_failed", error=str(exc))
        return await _render_portal(
            request, session, notice="Не удалось начать оплату. Попробуй позже."
        )
    return RedirectResponse(url=url, status_code=303)


@router.post("/billing/webhook", response_model=None)
async def billing_webhook(request: Request) -> Response:
    """Вебхук ЮKassa. Подписи нет — подлинность гарантирует re-GET платежа через
    наш secret (см. service.activate_from_payment). ВСЕГДА отвечаем 2xx, иначе
    ЮKassa будет ретраить; ошибку логируем и глотаем."""
    try:
        body = await request.json()
        pid = body["object"]["id"]
        await service.activate_from_payment(pid)
    except Exception as exc:  # noqa: BLE001 — любая ошибка не должна вызвать ретрай-шторм
        log.warning("billing.webhook_failed", error=str(exc))
    return Response(status_code=200)


@router.post("/billing/cancel", response_model=None)
async def billing_cancel(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> Response:
    """Отменить автопродление подписки (доступ — до конца оплаченного периода)."""
    await service.cancel_subscription(int(session["user_id"]))
    return RedirectResponse(url="/billing", status_code=303)


@router.get("/api/v1/license/{key}", response_model=None)
async def license_validate(request: Request, key: str) -> JSONResponse:
    """Публичная валидация лицензии для чужого self-host. Rate-limit по IP.
    Не светим чужие данные — только факт активности, план, статус, срок."""
    ip = request.client.host if request.client else "?"
    if not rate_limit.allow(f"lic:{ip}", 30, 60):
        return JSONResponse({"valid": False}, status_code=429)
    async with get_connection() as conn:
        sub = await repo.get_subscription_by_license(conn, key)
    if subscription_active(sub):
        return JSONResponse({
            "valid": True,
            "plan": (sub or {}).get("plan"),
            "status": (sub or {}).get("status"),
            "expires_at": (sub or {}).get("current_period_end"),
        })
    return JSONResponse({"valid": False})
