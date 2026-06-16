"""Аналитика за период — агрегатор для страницы /analytics (BUILD_PLAN B1).

Считает по локальным дням за последние N дней: посуточная динамика (скрины,
минуты звука, использований ИИ, токены), итоги, покрытие захвата (сколько дней
с данными), топ-приложения, использование ИИ из llm_usage (по kind/провайдеру).

Фильтрация и группировка — через ``date(col,'localtime')``: эта SQLite-функция
парсит и 'YYYY-MM-DD HH:MM:SS' (datetime('now')), и ISO с 'T'+tz, и приводит к
ЛОКАЛЬНОЙ дате — формат-агностично (колонки в схеме разнородны). Не hot-path,
поэтому индекс по времени здесь не критичен.
"""

from __future__ import annotations

from typing import Any

from app.day_overview import shift_day, today_iso
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.analytics")

_ALLOWED_DAYS = (7, 30, 90)


def _day_range(days: int) -> list[str]:
    """Список ISO-дат [start..today] длиной days (включая сегодня)."""
    end = today_iso()
    out = [shift_day(end, -(days - 1 - i)) for i in range(days)]
    return out


async def _grouped(conn: Any, sql: str, params: tuple) -> dict[str, float]:
    """Выполнить GROUP BY date(...) запрос → {day_iso: value}. {} при ошибке."""
    try:
        cur = await conn.execute(sql, params)
        return {str(r[0]): float(r[1] or 0) for r in await cur.fetchall() if r[0]}
    except Exception as exc:  # noqa: BLE001 — таблица/колонка может отсутствовать
        log.debug("analytics.grouped_failed", sql=sql[:60], error=str(exc))
        return {}


async def get_analytics(days: int = 30, user_id: int | None = None) -> dict[str, Any]:
    """Сводная аналитика за последние ``days`` дней (7/30/90)."""
    if days not in _ALLOWED_DAYS:
        days = 30
    day_list = _day_range(days)
    start, end = day_list[0], day_list[-1]
    span = (start, end)

    async with get_connection() as conn:
        screens = await _grouped(
            conn, "SELECT date(captured_at,'localtime') d, COUNT(*) FROM screenshots "
            "WHERE date(captured_at,'localtime') BETWEEN ? AND ? GROUP BY d", span)
        audio = await _grouped(
            conn, "SELECT date(captured_at,'localtime') d, COALESCE(SUM(duration_seconds),0) "
            "FROM audio_segment WHERE date(captured_at,'localtime') BETWEEN ? AND ? GROUP BY d", span)
        chat_ai = await _grouped(
            conn, "SELECT date(created_at,'localtime') d, COUNT(*) FROM chat_message "
            "WHERE role='assistant' AND date(created_at,'localtime') BETWEEN ? AND ? GROUP BY d", span)
        tokens = await _grouped(
            conn, "SELECT date(created_at,'localtime') d, COALESCE(SUM(input_tokens+output_tokens),0) "
            "FROM chat_message WHERE date(created_at,'localtime') BETWEEN ? AND ? GROUP BY d", span)
        tools = await _grouped(
            conn, "SELECT date(started_at,'localtime') d, COUNT(*) FROM tool_execution "
            "WHERE date(started_at,'localtime') BETWEEN ? AND ? GROUP BY d", span)
        voice = await _grouped(
            conn, "SELECT date(completed_at,'localtime') d, COUNT(*) FROM voice_tts "
            "WHERE status='done' AND completed_at IS NOT NULL "
            "AND date(completed_at,'localtime') BETWEEN ? AND ? GROUP BY d", span)

        # топ-приложения за период
        top_apps: list[dict[str, Any]] = []
        try:
            cur = await conn.execute(
                "SELECT app_name, COUNT(*) c FROM screenshots WHERE date(captured_at,'localtime') "
                "BETWEEN ? AND ? AND app_name IS NOT NULL AND app_name != '' "
                "GROUP BY app_name ORDER BY c DESC LIMIT 12", span)
            top_apps = [{"app": str(r[0]), "count": int(r[1])} for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            log.debug("analytics.top_apps_failed", error=str(exc))

        # использование ИИ (llm_usage) по kind и провайдеру
        llm_kind: list[dict[str, Any]] = []
        llm_provider: list[dict[str, Any]] = []
        try:
            cur = await conn.execute(
                "SELECT kind, COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
                "FROM llm_usage WHERE date(ts,'localtime') BETWEEN ? AND ? GROUP BY kind ORDER BY 2 DESC", span)
            llm_kind = [{"kind": str(r[0]), "calls": int(r[1]), "input": int(r[2]), "output": int(r[3])}
                        for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT provider, COUNT(*) FROM llm_usage WHERE date(ts,'localtime') BETWEEN ? AND ? "
                "GROUP BY provider ORDER BY 2 DESC", span)
            llm_provider = [{"provider": str(r[0] or '—'), "calls": int(r[1])} for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            log.debug("analytics.llm_failed", error=str(exc))

    # посуточная серия (все дни, включая нулевые — для графика)
    series = []
    for d in day_list:
        ai = int(chat_ai.get(d, 0) + tools.get(d, 0) + voice.get(d, 0))
        series.append({
            "day": d,
            "screenshots": int(screens.get(d, 0)),
            "audio_min": round(audio.get(d, 0) / 60, 1),
            "ai_uses": ai,
            "tokens": int(tokens.get(d, 0)),
        })

    total_screens = sum(s["screenshots"] for s in series)
    total_audio_min = round(sum(s["audio_min"] for s in series), 1)
    total_ai = sum(s["ai_uses"] for s in series)
    total_tokens = sum(s["tokens"] for s in series)
    capture_days = sum(1 for s in series if s["screenshots"] > 0 or s["audio_min"] > 0)

    return {
        "days": days,
        "start": start,
        "end": end,
        "series": series,
        "max_screens": max((s["screenshots"] for s in series), default=0),
        "max_ai": max((s["ai_uses"] for s in series), default=0),
        "totals": {
            "screenshots": total_screens,
            "audio_min": total_audio_min,
            "ai_uses": total_ai,
            "tokens": total_tokens,
            "capture_days": capture_days,
            "coverage_pct": round(100 * capture_days / days) if days else 0,
        },
        "top_apps": top_apps,
        "llm_by_kind": llm_kind,
        "llm_by_provider": llm_provider,
    }


__all__ = ["get_analytics"]
