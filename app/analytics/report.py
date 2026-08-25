"""Сборка вьюмодели дашборда: один вызов — вся страница.

Здесь нет SQL (он в :mod:`app.analytics.store`) и нет решений о приватности
(они в :mod:`app.analytics.capture`) — только «какие числа показать и как их
честно подписать».

Два принципа, ради которых модуль вообще существует
--------------------------------------------------
1. **Ленивое обслуживание перед чтением.** Свёртка закрытых суток и вычистка
   окна дёргаются отсюда, а не из воркера: страницу открывает один человек и
   редко, а лишний фоновый цикл на этом сервере стоит дороже, чем полсекунды
   на его же дашборде. Обе операции идемпотентны и полностью проглатывают свои
   ошибки: развалившаяся свёртка не имеет права не дать посмотреть цифры.
2. **Отчёт обязан рассказывать про свои дыры.** ``coverage`` уезжает в шаблон
   и превращается в текст «данные с такого-то числа». Пустой график за 30 дней
   на инстансе, где аналитика включилась вчера, читается как «трафика нет» —
   это не отсутствие данных, это ложные данные.

Спарклайны рисуются ИНЛАЙНОВЫМ SVG (:func:`sparkline_points`,
:func:`bar_geometry`). Никакой библиотеки графиков: CSP инстанса
(``script-src 'self'``) внешние скрипты запрещает, а тянуть чарт-библиотеку в
``/static`` ради шести полосок — лишние сотни килобайт на странице, которую
открывают раз в неделю.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.analytics import capture, funnel, store
from app.logging_setup import get_logger

log = get_logger("persona.analytics.report")

#: Окна, которые владелец просил: «сегодня / 7 дней / 30 дней».
WINDOWS: tuple[int, ...] = (1, 7, 30)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _span(days: int, today: str | None = None) -> tuple[str, str]:
    end = date.fromisoformat(today or _today())
    start = end - timedelta(days=max(1, days) - 1)
    return start.isoformat(), end.isoformat()


def sparkline_points(values: Sequence[int], width: int = 220, height: int = 40) -> str:
    """Строка ``points`` для ``<polyline>``. Пусто → пустая строка (шаблон скроет)."""
    if not values:
        return ""
    top = max(values) or 1
    if len(values) == 1:
        return f"0,{height - (values[0] / top) * height:.1f}"
    step = width / (len(values) - 1)
    return " ".join(
        f"{i * step:.1f},{height - (v / top) * height:.1f}" for i, v in enumerate(values)
    )


def bar_geometry(
    values: Sequence[int], width: int = 220, height: int = 40
) -> list[dict[str, float]]:
    """Прямоугольники столбчатой диаграммы для инлайнового SVG."""
    if not values:
        return []
    top = max(values) or 1
    slot = width / len(values)
    bar = max(1.0, slot * 0.7)
    out: list[dict[str, float]] = []
    for i, value in enumerate(values):
        h = (value / top) * height
        out.append(
            {
                "x": round(i * slot + (slot - bar) / 2, 2),
                "y": round(height - h, 2),
                "w": round(bar, 2),
                "h": round(max(h, 0.0), 2),
                "value": value,
            }
        )
    return out


async def _maintain(today: str) -> dict[str, Any]:
    """Свернуть закрытые сутки и вычистить окно. Ошибки не всплывают наружу."""
    result: dict[str, Any] = {"rolled": [], "purged": 0, "error": ""}
    try:
        result["rolled"] = await store.rollup_closed_days(today=today)
    except Exception as exc:  # noqa: BLE001 — отчёт важнее обслуживания
        result["error"] = str(exc)
        log.warning("analytics.rollup_failed", error=str(exc))
    try:
        result["purged"] = await store.purge_old_events(today=today)
    except Exception as exc:  # noqa: BLE001
        result["error"] = result["error"] or str(exc)
        log.warning("analytics.purge_failed", error=str(exc))
    return result


async def build_dashboard(
    *, days: int = 30, owner_id: int | None = None, today: str | None = None
) -> dict[str, Any]:
    """Всё, что показывает ``/root/analytics``, одним словарём."""
    today = today or _today()
    maintenance = await _maintain(today)
    day_from, day_to = _span(days, today)
    all_days = list(store.iter_days(day_from, day_to))

    coverage = await store.coverage()
    registrations = await store.registrations_by_day(day_from, day_to)
    reg_series = [registrations.get(d, 0) for d in all_days]

    sessions_by_day = await store.unique_sessions_by_day(day_from, day_to)
    dau_by_day = await store.active_users_by_day(day_from, day_to)

    week_from, _ = _span(7, today)
    month_from, _ = _span(30, today)

    top_pages = await store.aggregate(
        day_from, day_to, group=("path", "role"), kind=capture.KIND_VIEW, limit=60
    )
    # Свернём в «путь → по ролям», чтобы владелец сразу видел, что открывает
    # ОН, а что — участники. Это была прямая просьба: без разреза по ролям
    # верх списка всегда занимают его собственные страницы.
    pages: dict[str, dict[str, Any]] = {}
    for row in top_pages:
        item = pages.setdefault(
            row["path"],
            {"path": row["path"], "total": 0, "owner": 0, "member": 0, "anonymous": 0},
        )
        item["total"] += int(row["hits"])
        item[row["role"]] = item.get(row["role"], 0) + int(row["hits"])
    top_pages_view = sorted(pages.values(), key=lambda r: -r["total"])[:20]

    clicks = await store.aggregate(
        day_from, day_to, group=("label", "kind"), limit=40
    )
    clicks = [c for c in clicks if c["kind"] != capture.KIND_VIEW and c["label"]][:20]

    referrers = await store.aggregate(
        day_from, day_to, group=("referrer_host",), kind=capture.KIND_VIEW, limit=30
    )
    referrers = [r for r in referrers if r["referrer_host"]][:15]

    devices = await store.aggregate(
        day_from, day_to, group=("device",), kind=capture.KIND_VIEW
    )
    roles = await store.aggregate(
        day_from, day_to, group=("role",), kind=capture.KIND_VIEW
    )

    return {
        "today": today,
        "days": days,
        "day_from": day_from,
        "day_to": day_to,
        "all_days": all_days,
        "enabled": capture.is_enabled(),
        "coverage": coverage,
        "maintenance": maintenance,
        "buffer": {"pending": capture.buffered(), "dropped": capture.dropped()},
        "retention_days": await store.retention_window(),
        "registrations": {
            "series": reg_series,
            "bars": bar_geometry(reg_series),
            "today": registrations.get(today, 0),
            "last_7": sum(registrations.get(d, 0) for d in store.iter_days(week_from, today)),
            "last_30": sum(
                registrations.get(d, 0) for d in store.iter_days(month_from, today)
            ),
            "total": sum(reg_series),
        },
        "activity": {
            "dau_series": [dau_by_day.get(d, 0) for d in all_days],
            "dau_points": sparkline_points([dau_by_day.get(d, 0) for d in all_days]),
            "sessions_series": [sessions_by_day.get(d, 0) for d in all_days],
            "sessions_points": sparkline_points(
                [sessions_by_day.get(d, 0) for d in all_days]
            ),
            "dau_today": dau_by_day.get(today, 0),
            "wau": await store.weekly_active_users(week_from, today),
        },
        "retention": await store.cohort_retention(weeks=4, today=today),
        "funnel": await funnel.build_funnel(day_from, day_to, owner_id=owner_id),
        "top_pages": top_pages_view,
        "clicks": clicks,
        "referrers": referrers,
        "devices": devices,
        "roles": roles,
        "live": await store.live_sessions(15),
    }


__all__ = ["WINDOWS", "bar_geometry", "build_dashboard", "sparkline_points"]
