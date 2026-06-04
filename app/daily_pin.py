"""Tier 5 memory: ultra-compact daily pin (v1.14).

One row per day. ~200 bytes plain text. Survives every retention sweep
in the project (audit-checked). 10-year archive = ~750 KB.

Heuristic-first: built from the day's hourly cards + raw counts. LLM
can later overwrite the ``pin`` field with a richer narrative if a
provider is configured. The schema records which source produced the
current value so a downgrade (LLM key revoked) doesn't silently lose
prior LLM-quality pins.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import aiosqlite

from app.hourly_card import _gather_apps, _gather_audio, _gather_top_words
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.daily_pin")


async def _build_heuristic_pin(
    conn: aiosqlite.Connection,
    *,
    day: date,
) -> dict[str, object] | None:
    """Compute the heuristic pin for ``day``. Returns ``None`` if empty day."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)

    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at <= ?",
        (start.isoformat(), end.isoformat()),
    )
    row = await cursor.fetchone()
    screen_count = int(row["n"]) if row else 0

    audio_seconds, _excerpt = await _gather_audio(conn, start, end)
    if screen_count == 0 and audio_seconds == 0:
        return None

    apps = await _gather_apps(conn, start, end)
    top_words = await _gather_top_words(conn, start, end)

    app_names = [str(a["app"]) for a in apps[:5]]
    voice_minutes = audio_seconds // 60

    # Compact one-line pin. Keep < 500 chars. No newlines so the row
    # fits a single search-result line.
    parts: list[str] = []
    if app_names:
        parts.append("apps: " + ", ".join(app_names))
    parts.append(f"screens: {screen_count}")
    if voice_minutes:
        parts.append(f"voice: {voice_minutes}m")
    if top_words:
        parts.append("kw: " + ", ".join(top_words[:8]))
    pin = " · ".join(parts)[:500]

    return {
        "day": day.isoformat(),
        "pin": pin,
        "apps": ", ".join(app_names),
        "voice_minutes": voice_minutes,
        "screen_count": screen_count,
        "source": "heuristic",
    }


async def write_pin_for_day(day: date | None = None) -> dict[str, object] | None:
    """Compute + upsert the daily pin for ``day`` (default = yesterday UTC).

    Returns the row dict or ``None`` if the day was empty.
    """
    target = day or (datetime.now(tz=UTC).date() - timedelta(days=1))
    async with get_connection() as conn:
        payload = await _build_heuristic_pin(conn, day=target)
        if payload is None:
            log.debug("daily_pin.skipped_empty", day=target.isoformat())
            return None

        await conn.execute(
            "INSERT INTO daily_pin (day, pin, apps, voice_minutes, screen_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET "
            "  pin = excluded.pin, "
            "  apps = excluded.apps, "
            "  voice_minutes = excluded.voice_minutes, "
            "  screen_count = excluded.screen_count, "
            "  source = excluded.source, "
            "  updated_at = datetime('now')",
            (
                payload["day"],
                payload["pin"],
                payload["apps"],
                payload["voice_minutes"],
                payload["screen_count"],
                payload["source"],
            ),
        )
        await conn.commit()

    log.info(
        "daily_pin.written",
        day=target.isoformat(),
        screens=payload["screen_count"],
        voice_minutes=payload["voice_minutes"],
        chars=len(str(payload["pin"])),
    )
    return payload


async def list_pins(limit: int = 365) -> list[dict[str, object]]:
    """Return the most recent N pins (newest first). Default 1 year."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day, pin, apps, voice_minutes, screen_count, source, "
            "       created_at, updated_at FROM daily_pin "
            "ORDER BY day DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()
    return [
        {
            "day": str(r["day"]),
            "pin": str(r["pin"]),
            "apps": str(r["apps"] or ""),
            "voice_minutes": int(r["voice_minutes"] or 0),
            "screen_count": int(r["screen_count"] or 0),
            "source": str(r["source"] or "heuristic"),
            "created_at": str(r["created_at"] or ""),
            "updated_at": str(r["updated_at"] or ""),
        }
        for r in rows
    ]
