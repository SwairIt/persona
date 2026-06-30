"""Кабинет биллинга /billing — план, лицензионный ключ, апгрейд.

Сюда попадают НЕ-владельцы после входа (вместо тупиковой /pending): покупатель
видит свой план/триал и лицензионный ключ, который вставляет в свою Persona.
Приложение (данные владельца) им недоступно — гейт пускает только /billing.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import __version__
from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.auth.users import normalise_email
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
    async with get_connection() as conn:
        payments = await repo.list_payments(conn, uid, limit=10)
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
            "payments": payments,
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


def _webhook_ip_allowed(request: Request) -> bool:
    """Defense-in-depth: пускаем вебхук только из IP-диапазонов ЮKassa. Подписи у
    вебхука нет, и без этого фильтра кто угодно слал бы произвольные payment_id,
    заставляя нас дёргать аутентифицированный GET к ЮKassa (SSRF-усилитель + DoS).
    Реальный IP берём через trusted-proxy XFF (`_client_ip`) — за devtunnel прямой
    peer = 127.0.0.1, настоящий адрес ЮKassa приходит в X-Forwarded-For."""
    from ipaddress import ip_address, ip_network  # noqa: PLC0415

    from app.billing.yookassa import WEBHOOK_IP_RANGES  # noqa: PLC0415
    from app.web.routes.auth import _client_ip  # noqa: PLC0415

    try:
        addr = ip_address(_client_ip(request))
    except ValueError:
        return False
    return any(addr in ip_network(cidr) for cidr in WEBHOOK_IP_RANGES)


@router.post("/billing/webhook", response_model=None)
async def billing_webhook(request: Request) -> Response:
    """Вебхук ЮKassa. Подписи нет — подлинность гарантирует re-GET платежа через
    наш secret (см. service.activate_from_payment) + IP-allowlist ЮKassa (см.
    _webhook_ip_allowed). Для разрешённых IP ВСЕГДА отвечаем 2xx, иначе ЮKassa
    будет ретраить; ошибку логируем и глотаем."""
    if not _webhook_ip_allowed(request):
        log.warning("billing.webhook_forbidden_ip")
        return Response(status_code=403)
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


@router.get("/settings/billing-admin", response_class=HTMLResponse, response_model=None)
async def billing_admin(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    notice: str | None = None,
) -> HTMLResponse:
    """Биллинг-админка владельца: все подписки + ручной грант Pro. Только owner."""
    uid = int(session["user_id"])
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="Только владелец")
    async with get_connection() as conn:
        subscriptions = await repo.list_all_subscriptions(conn)
    return templates.TemplateResponse(
        request,
        "billing_admin.html",
        {
            "title": "Биллинг — админ",
            "app_version": __version__,
            "active_nav": "settings",
            "is_owner": True,
            "session": session,
            "subscriptions": subscriptions,
            "notice": notice,
        },
    )


@router.post("/settings/billing-admin/grant", response_model=None)
async def billing_admin_grant(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    email: Annotated[str, Form()],
    days: Annotated[int, Form()],
) -> Response:
    """Выдать пользователю (по email) ручной Pro-грант на N дней. Только owner."""
    uid = int(session["user_id"])
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="Только владелец")
    try:
        norm = normalise_email(email)
    except ValueError:
        return RedirectResponse(url="/settings/billing-admin?notice=bad_email", status_code=303)
    async with get_connection() as conn:
        cur = await conn.execute("SELECT id FROM users WHERE email = ?", (norm,))
        row = await cur.fetchone()
    if row is None:
        return RedirectResponse(url="/settings/billing-admin?notice=not_found", status_code=303)
    await service.grant_pro(int(row["id"]), max(1, int(days)))
    return RedirectResponse(url="/settings/billing-admin?notice=granted", status_code=303)


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
