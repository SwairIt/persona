"""Первосторонняя аналитика: дашборд владельца + приёмник клиентских действий.

Почему отдельная страница ``/root/analytics``, а не вкладка внутри ``/root``
---------------------------------------------------------------------------
``/root`` — это ПУЛЬТ: он тянет живые логи (опрос ``/root/logs/recent.json``
каждые несколько секунд), сводку здоровья и список пользователей. Его открывают,
когда что-то сломалось, и держат открытым. Аналитика — противоположный режим:
тяжёлое агрегатное чтение плюс ленивое обслуживание (свёртка суток + вычистка
окна), которое незачем запускать каждый раз, когда владелец пошёл смотреть
логи. Слив их в одну страницу означал бы, что пульт с живыми логами платит за
свёртку тридцати суток при каждом открытии, а аналитика перерисовывается
поллером логов. Разные режимы чтения — разные страницы; вход в аналитику
стоит прямо на ``/root`` и в чипе аккаунта.

Гейт
----
``/root`` целиком закрыт ``_OWNER_ONLY_PREFIXES`` в
``app/web/middleware/auth_gate.py``, но каждый хендлер ЗАНОВО проверяет
владельца — ровно как ``root_control.py``. Это defence-in-depth: гейт можно
переконфигурировать, роут — нет.

Роуты (три, и почему их не два и не пять)
-----------------------------------------
* ``GET  /root/analytics``          — сама страница;
* ``POST /api/track``               — приёмник кликов/сабмитов/исходящих ссылок.
  Нужен отдельный, потому что просмотры страниц берутся из middleware, а клик
  по кнопке сервер не видит в принципе. Принимает ПАЧКУ событий (клиент
  копит и шлёт разом), чтобы не превращать активную страницу в поток запросов;
* ``POST /root/analytics/settings`` — рубильник и окно хранения. Без него
  «выключить аналитику» означало бы правку kv руками в базе, а выключатель,
  до которого нельзя дотянуться, выключателем не является.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.analytics import capture
from app.auth import current_user_optional, current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["analytics"])
log = get_logger("persona.analytics.routes")

#: Сколько событий принимаем за один вызов приёмника. Больше — это уже не
#: «пачка кликов», а попытка залить нам таблицу.
_MAX_BATCH = 20


async def _require_owner(session: SessionRecord) -> int:
    uid = session["user_id"]
    if not await is_owner(uid):
        raise HTTPException(status_code=403, detail="только для владельца")
    return uid


@router.get("/root/analytics", response_class=HTMLResponse)
async def analytics_dashboard(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    days: int = 30,
) -> HTMLResponse:
    """Дашборд владельца. Читает свёртку, а не сырьё (см. app/analytics/store)."""
    owner_id = await _require_owner(session)
    days = 7 if days == 7 else (1 if days == 1 else 30)
    from app.analytics import report  # noqa: PLC0415 — тяжёлый модуль, не на импорте

    await capture.refresh_state()
    try:
        data: dict[str, Any] = await report.build_dashboard(
            days=days, owner_id=owner_id
        )
        error = ""
    except Exception as exc:  # noqa: BLE001 — пустая страница лучше 500
        log.warning("analytics.dashboard_failed", error=str(exc))
        data, error = {}, str(exc)
    return templates.TemplateResponse(
        request,
        "root_analytics.html",
        {
            "title": "Аналитика — пульт владельца",
            "active_nav": "root",
            "is_owner": True,
            "a": data,
            "error": error,
            "days": days,
        },
    )


@router.post("/root/analytics/settings", response_model=None)
async def analytics_settings(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
):
    """Рубильник ``analytics_enabled`` и окно ``analytics_retention_days``."""
    await _require_owner(session)
    form = await request.form()
    enabled = "1" if str(form.get("enabled") or "") in ("1", "on", "true") else "0"
    raw_days = str(form.get("retention_days") or "").strip()
    from app.analytics import store  # noqa: PLC0415

    try:
        days: int | None = max(1, min(3650, int(raw_days)))
    except ValueError:
        days = None
    await store.save_settings(enabled=enabled, retention_days=days)
    capture.reset_cache()
    await capture.refresh_state()
    return RedirectResponse(url="/root/analytics", status_code=303)


@router.post("/api/track", response_class=JSONResponse)
async def track(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> JSONResponse:
    """Приёмник кликов, сабмитов и исходящих ссылок от ``static/track.js``.

    Клиенту НЕ доверяем ничего, кроме подписи элемента и типа события:

    * ``path`` берётся не из тела, а нормализуется по таблице роутов из
      присланного пути — то есть произвольную строку в колонку путей вписать
      нельзя;
    * ``role``/``user_id`` берутся из сессии, а не из тела;
    * ``label`` режется по длине, содержимое полей формы не принимается вовсе
      (у сабмита подпись — это ``data-track`` или ``id`` формы, не её данные).

    Ответ всегда 200 (даже когда аналитика выключена): счётчик не должен
    сообщать странице ничего, на что она стала бы реагировать.
    """
    await capture.refresh_state()
    accepted = 0
    if capture.is_enabled():
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — мусор в теле не повод для 4xx
            payload = {}
        events = payload.get("events") if isinstance(payload, dict) else None
        uid = session["user_id"] if session else None
        role = capture.ROLE_ANONYMOUS
        if uid is not None:
            role = (
                capture.ROLE_OWNER if await is_owner(uid) else capture.ROLE_MEMBER
            )
        consented = (
            request.cookies.get(capture.CONSENT_COOKIE) == capture.CONSENT_GRANTED
        )
        # Действия анонима без согласия НЕ пишем вовсе. Просмотр страницы можно
        # посчитать обезличенно (это счётчик посещений), а «на что человек
        # нажал» — уже поведение конкретного посетителя, и без согласия мы его
        # не собираем. См. докстринг app/analytics/capture.py.
        if isinstance(events, list) and (uid is not None or consented):
            device = capture.device_class(request.headers.get("user-agent", ""))
            for item in events[:_MAX_BATCH]:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "")
                if kind not in (
                    capture.KIND_CLICK,
                    capture.KIND_SUBMIT,
                    capture.KIND_OUTBOUND,
                ):
                    continue
                raw_path = str(item.get("path") or "/")[:300]
                label = str(item.get("label") or "")[:120]
                if kind == capture.KIND_OUTBOUND:
                    label = capture.referrer_host(label) or "—"
                if capture.record(
                    kind=kind,
                    path=capture.normalise_path(request.app, raw_path),
                    role=role,
                    device=device,
                    label=label,
                    user_id=uid,
                ):
                    accepted += 1
    return JSONResponse({"ok": True, "accepted": accepted})


__all__ = ["router"]
