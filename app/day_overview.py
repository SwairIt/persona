"""Агрегатор «обзор дня» — единый источник статистики за один календарный день.

В Persona нет таблицы «день»: данные размазаны по screenshots / audio_segment /
chat_message / tool_execution / llm_usage / hourly_card / day_tldr / budget. Этот
модуль собирает их в один объект для страницы ``/day/{date}`` (ROADMAP BUILD_PLAN A1):
сколько скринов, был ли/сколько записан звук, сколько использовался ИИ (вызовы+токены),
часы активности, часовые карточки, TL;DR, бюджет хранилища, топ-приложения.

Каждый блок обёрнут в try/except: схема большая и местами опциональная (таблица может
отсутствовать на старой БД) — обзор дня НЕ должен падать целиком из-за одного блока.
Границы дня берём по ЛОКАЛЬНОЙ полуночи (как day_json/timeline), чтобы цифры совпадали
с тем, что видит пользователь.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.day_overview")


def day_bounds_utc(day_iso: str) -> tuple[str, str]:
    """``YYYY-MM-DD`` (локальная) → полуоткрытое UTC-окно [since, until) в ISO."""
    parsed = datetime.strptime(day_iso, "%Y-%m-%d").date()
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(parsed.year, parsed.month, parsed.day, tzinfo=tz)
    until_local = since_local + timedelta(days=1)
    return since_local.astimezone(UTC).isoformat(), until_local.astimezone(UTC).isoformat()


def today_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def shift_day(day_iso: str, days: int) -> str:
    parsed = datetime.strptime(day_iso, "%Y-%m-%d").date()
    return (parsed + timedelta(days=days)).isoformat()


async def _scalar(conn: Any, sql: str, params: tuple = ()) -> Any:
    try:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        if not row:
            return None
        return row[0]
    except Exception as exc:  # noqa: BLE001 — таблица/колонка может отсутствовать
        log.debug("day_overview.scalar_failed", sql=sql[:60], error=str(exc))
        return None


async def get_day_overview(day_iso: str, user_id: int | None = None) -> dict[str, Any]:
    """Собрать обзор одного дня. user_id=None → агрегировать по всем (single-owner)."""
    since, until = day_bounds_utc(day_iso)
    out: dict[str, Any] = {
        "day": day_iso,
        "prev_day": shift_day(day_iso, -1),
        "next_day": shift_day(day_iso, 1),
        "is_today": day_iso == today_iso(),
    }

    async with get_connection() as conn:
        # ── Скриншоты + OCR ──
        out["screenshots"] = int(await _scalar(
            conn, "SELECT COUNT(*) FROM screenshots WHERE captured_at >= ? AND captured_at < ?",
            (since, until)) or 0)
        out["ocr_done"] = int(await _scalar(
            conn, "SELECT COUNT(*) FROM screenshots WHERE captured_at >= ? AND captured_at < ? "
            "AND ocr_status = 'done'", (since, until)) or 0)
        out["active_hours"] = int(await _scalar(
            conn, "SELECT COUNT(DISTINCT strftime('%H', captured_at, 'localtime')) FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ?", (since, until)) or 0)
        out["first_capture"] = await _scalar(
            conn, "SELECT strftime('%H:%M', MIN(captured_at), 'localtime') FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ?", (since, until))
        out["last_capture"] = await _scalar(
            conn, "SELECT strftime('%H:%M', MAX(captured_at), 'localtime') FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ?", (since, until))

        # топ-приложения дня
        out["top_apps"] = []
        try:
            cur = await conn.execute(
                "SELECT app_name, COUNT(*) c FROM screenshots "
                "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL AND app_name != '' "
                "GROUP BY app_name ORDER BY c DESC LIMIT 8", (since, until))
            out["top_apps"] = [{"app": str(r[0]), "count": int(r[1])} for r in await cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            log.debug("day_overview.top_apps_failed", error=str(exc))

        # ── Звук ── (audio_segment: captured_at + duration_seconds после миграций)
        out["audio_seconds"] = int(await _scalar(
            conn, "SELECT COALESCE(SUM(duration_seconds),0) FROM audio_segment "
            "WHERE captured_at >= ? AND captured_at < ?", (since, until)) or 0)
        out["audio_segments"] = int(await _scalar(
            conn, "SELECT COUNT(*) FROM audio_segment WHERE captured_at >= ? AND captured_at < ?",
            (since, until)) or 0)

        # ── Чат / использование ИИ ──
        if user_id is not None:
            sess_filter = "AND session_id IN (SELECT id FROM chat_session WHERE user_id = ?)"
            extra: tuple = (user_id,)
        else:
            sess_filter, extra = "", ()
        # datetime()-обёртка: created_at может быть в формате 'YYYY-MM-DD HH:MM:SS'
        # (datetime('now')) ИЛИ ISO с 'T'+tz — datetime() нормализует оба к UTC.
        _cm_win = f"datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?) {sess_filter}"
        out["chat_messages"] = int(await _scalar(
            conn, f"SELECT COUNT(*) FROM chat_message WHERE {_cm_win}",
            (since, until, *extra)) or 0)
        out["ai_replies"] = int(await _scalar(
            conn, f"SELECT COUNT(*) FROM chat_message WHERE role='assistant' AND {_cm_win}",
            (since, until, *extra)) or 0)
        out["input_tokens"] = int(await _scalar(
            conn, f"SELECT COALESCE(SUM(input_tokens),0) FROM chat_message WHERE {_cm_win}",
            (since, until, *extra)) or 0)
        out["output_tokens"] = int(await _scalar(
            conn, f"SELECT COALESCE(SUM(output_tokens),0) FROM chat_message WHERE {_cm_win}",
            (since, until, *extra)) or 0)

        # инструменты + llm_usage по kind
        out["tool_calls"] = int(await _scalar(
            conn, "SELECT COUNT(*) FROM tool_execution WHERE datetime(started_at) >= datetime(?) "
            "AND datetime(started_at) < datetime(?)", (since, until)) or 0)
        out["llm_by_kind"] = []
        try:
            cur = await conn.execute(
                "SELECT kind, COUNT(*) calls, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
                "FROM llm_usage WHERE ts >= ? AND ts < ? GROUP BY kind ORDER BY calls DESC",
                (since, until))
            out["llm_by_kind"] = [
                {"kind": str(r[0]), "calls": int(r[1]), "input": int(r[2]), "output": int(r[3])}
                for r in await cur.fetchall()
            ]
        except Exception as exc:  # noqa: BLE001
            log.debug("day_overview.llm_kind_failed", error=str(exc))
        out["voice_replies"] = int(await _scalar(
            conn, "SELECT COUNT(*) FROM voice_tts WHERE status='done' AND completed_at IS NOT NULL "
            "AND datetime(completed_at) >= datetime(?) AND datetime(completed_at) < datetime(?)",
            (since, until)) or 0)
        # суммарно «использований ИИ» за день (ответы чата + голос + вызовы инструментов)
        out["ai_uses"] = out["ai_replies"] + out["voice_replies"] + out["tool_calls"]

        # ── Часовые карточки ──
        out["hourly_cards"] = []
        try:
            cur = await conn.execute(
                "SELECT hour_start, summary, screen_count, audio_seconds FROM hourly_card "
                "WHERE hour_start >= ? AND hour_start < ? ORDER BY hour_start ASC",
                (since[:19], until[:19]))  # hour_start без таймзоны (ISO до секунд)
            for r in await cur.fetchall():
                out["hourly_cards"].append({
                    "hour_start": str(r[0]),
                    "summary": str(r[1] or ""),
                    "screen_count": int(r[2] or 0),
                    "audio_seconds": int(r[3] or 0),
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("day_overview.hourly_failed", error=str(exc))

        # ── TL;DR + бюджет ──
        out["tldr"] = await _scalar(conn, "SELECT tldr FROM day_tldr WHERE day = ?", (day_iso,))
        out["budget_bytes"] = int(await _scalar(
            conn, "SELECT COALESCE(thumbnails_bytes,0)+COALESCE(audio_bytes,0)+COALESCE(events_bytes,0)"
            "+COALESCE(ocr_text_bytes,0)+COALESCE(embeddings_bytes,0)+COALESCE(misc_bytes,0) "
            "FROM daily_budget_state WHERE day = ?", (day_iso,)) or 0)

    out["recorded"] = out["screenshots"] > 0 or out["audio_seconds"] > 0
    out["audio_minutes"] = round(out["audio_seconds"] / 60, 1)
    out["total_tokens"] = out["input_tokens"] + out["output_tokens"]
    return out


__all__ = ["get_day_overview", "day_bounds_utc", "today_iso", "shift_day"]
