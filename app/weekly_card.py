"""Tier 2 memory: weekly summary card (v1.15).

Compresses one ISO week (Monday → Sunday) of activity into a single
~5 KB markdown card. Built deterministically from ``hourly_card`` and
``daily_pin`` rows — no LLM required. Lives forever; never touched by
retention sweeps.

The card is keyed by ``week_start`` (Monday, YYYY-MM-DD). The window is
the full Monday 00:00:00 → Sunday 23:59:59 UTC range, matching how
``hourly_card`` buckets store ``hour_start``.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, cast

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.weekly_card")


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


def _bounds_utc(week_start: date) -> tuple[datetime, datetime]:
    """Return ``(monday 00:00:00 UTC, sunday 23:59:59 UTC)`` for the week."""
    start = datetime.combine(week_start, time.min, tzinfo=UTC)
    end = datetime.combine(
        week_start + timedelta(days=6), time(23, 59, 59), tzinfo=UTC
    )
    return start, end


async def _sum_hourly_totals(
    conn: aiosqlite.Connection,
    *,
    start: datetime,
    end: datetime,
) -> tuple[int, int]:
    """Sum ``screen_count`` and ``audio_seconds`` across hourly_card rows."""
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(screen_count), 0) AS screens, "
        "       COALESCE(SUM(audio_seconds), 0) AS audio_s "
        "FROM hourly_card "
        "WHERE hour_start >= ? AND hour_start <= ?",
        (start.isoformat(), end.isoformat()),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0, 0
    return int(row["screens"] or 0), int(row["audio_s"] or 0)


async def _gather_top_apps(
    conn: aiosqlite.Connection,
    *,
    start: datetime,
    end: datetime,
) -> list[dict[str, int | str]]:
    """Aggregate per-hour ``apps_json`` arrays into a top-5 app list."""
    cursor = await conn.execute(
        "SELECT apps_json FROM hourly_card "
        "WHERE apps_json IS NOT NULL AND apps_json != '' "
        "AND hour_start >= ? AND hour_start <= ?",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cursor.fetchall()
    counter: Counter[str] = Counter()
    for r in rows:
        raw = r["apps_json"]
        if raw is None:
            continue
        try:
            parsed = json.loads(str(raw))
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            name = entry.get("app")
            shots = entry.get("shots", 0)
            if not isinstance(name, str) or not name:
                continue
            try:
                counter[name] += int(shots)
            except (TypeError, ValueError):
                continue
    return [{"app": app, "shots": n} for app, n in counter.most_common(5)]


async def _count_days_active(
    conn: aiosqlite.Connection,
    *,
    start: datetime,
    end: datetime,
) -> int:
    """Count distinct UTC days with at least one screenshot in the window."""
    cursor = await conn.execute(
        "SELECT COUNT(DISTINCT DATE(captured_at)) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at <= ?",
        (start.isoformat(), end.isoformat()),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"] or 0)


async def _gather_week_keywords(
    conn: aiosqlite.Connection,
    *,
    start: datetime,
    end: datetime,
    week_start: date,
) -> list[str]:
    """Top OCR keywords drawn from daily_pin.apps + hourly_card.top_words."""
    counter: Counter[str] = Counter()

    cursor = await conn.execute(
        "SELECT top_words FROM hourly_card "
        "WHERE top_words IS NOT NULL AND top_words != '' "
        "AND hour_start >= ? AND hour_start <= ?",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cursor.fetchall()
    for r in rows:
        text = str(r["top_words"] or "")
        for chunk in text.split(","):
            word = chunk.strip().lower()
            if word:
                counter[word] += 1

    week_end = week_start + timedelta(days=6)
    cursor = await conn.execute(
        "SELECT apps FROM daily_pin "
        "WHERE apps IS NOT NULL AND apps != '' "
        "AND day >= ? AND day <= ?",
        (week_start.isoformat(), week_end.isoformat()),
    )
    rows = await cursor.fetchall()
    for r in rows:
        text = str(r["apps"] or "")
        for chunk in text.split(","):
            word = chunk.strip().lower()
            if word:
                counter[word] += 1

    return [w for w, _ in counter.most_common(15)]


def _render_markdown(
    *,
    week_start: date,
    week_end: date,
    days_active: int,
    total_screens: int,
    total_voice_minutes: int,
    top_apps: list[dict[str, int | str]],
    top_keywords: list[str],
) -> str:
    """Render a small markdown block describing the week."""
    lines = [
        f"## {week_start.isoformat()} — {week_end.isoformat()}",
        (
            f"{days_active} дней активности, "
            f"{total_screens} кадров, "
            f"{total_voice_minutes} минут речи"
        ),
    ]
    if top_apps:
        app_strs = ", ".join(f"**{a['app']}** ({a['shots']})" for a in top_apps)
        lines.append(f"- Apps: {app_strs}")
    if top_keywords:
        lines.append(f"- Keywords: {', '.join(top_keywords[:15])}")
    return "\n".join(lines)


async def build_card_for_week(
    week_start_date: date,
) -> dict[str, object] | None:
    """Compute + upsert the weekly card for the ISO week of ``week_start_date``.

    ``week_start_date`` may be any day inside the target week; the
    Monday is computed automatically. Returns the row dict on write or
    ``None`` if the week had zero screens AND zero audio.
    """
    week_start = _monday_of(week_start_date)
    week_end = week_start + timedelta(days=6)
    start_utc, end_utc = _bounds_utc(week_start)

    async with get_connection() as conn:
        total_screens, audio_seconds = await _sum_hourly_totals(
            conn, start=start_utc, end=end_utc
        )
        if total_screens == 0 and audio_seconds == 0:
            log.debug("weekly_card.skipped_empty", week_start=week_start.isoformat())
            return None

        total_voice_minutes = audio_seconds // 60
        top_apps = await _gather_top_apps(conn, start=start_utc, end=end_utc)
        days_active = await _count_days_active(
            conn, start=start_utc, end=end_utc
        )
        top_keywords = await _gather_week_keywords(
            conn, start=start_utc, end=end_utc, week_start=week_start
        )

        summary = _render_markdown(
            week_start=week_start,
            week_end=week_end,
            days_active=days_active,
            total_screens=total_screens,
            total_voice_minutes=total_voice_minutes,
            top_apps=top_apps,
            top_keywords=top_keywords,
        )

        payload: dict[str, object] = {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "summary": summary,
            "top_apps_json": json.dumps(top_apps, ensure_ascii=False),
            "total_screens": total_screens,
            "total_voice_minutes": total_voice_minutes,
            "days_active": days_active,
            "source": "heuristic",
        }

        await conn.execute(
            "INSERT OR REPLACE INTO weekly_card "
            "(week_start, week_end, summary, top_apps_json, total_screens, "
            " total_voice_minutes, days_active, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload["week_start"],
                payload["week_end"],
                payload["summary"],
                payload["top_apps_json"],
                payload["total_screens"],
                payload["total_voice_minutes"],
                payload["days_active"],
                payload["source"],
            ),
        )
        await conn.commit()

    log.info(
        "weekly_card.written",
        week_start=week_start.isoformat(),
        days_active=days_active,
        screens=total_screens,
        voice_minutes=total_voice_minutes,
        apps=len(top_apps),
        keywords=len(top_keywords),
    )
    payload["top_apps"] = top_apps
    return payload


async def list_weekly_cards(limit: int = 12) -> list[dict[str, object]]:
    """Return the most recent N weekly cards (newest first)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT week_start, week_end, summary, top_apps_json, "
            "       total_screens, total_voice_minutes, days_active, "
            "       source, created_at FROM weekly_card "
            "ORDER BY week_start DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()

    out: list[dict[str, object]] = []
    for r in rows:
        raw_apps = r["top_apps_json"]
        top_apps: list[dict[str, int | str]] = []
        if raw_apps:
            try:
                parsed = json.loads(str(raw_apps))
                if isinstance(parsed, list):
                    top_apps = cast("list[dict[str, int | str]]", parsed)
            except (TypeError, ValueError):
                top_apps = []
        out.append(
            {
                "week_start": str(r["week_start"]),
                "week_end": str(r["week_end"]),
                "summary": str(r["summary"]),
                "top_apps": top_apps,
                "total_screens": int(r["total_screens"] or 0),
                "total_voice_minutes": int(r["total_voice_minutes"] or 0),
                "days_active": int(r["days_active"] or 0),
                "source": str(r["source"] or "heuristic"),
                "created_at": str(r["created_at"] or ""),
            }
        )
    return out


__all__ = ["build_card_for_week", "list_weekly_cards"]
