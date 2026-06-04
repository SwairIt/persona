"""Daily storage-budget enforcer (v1.13).

The capture loop, audio worker, and event log all call into this module
when they're about to write bytes. The module:

1. Bumps the per-day per-bucket counter.
2. Computes today's projected end-of-day usage.
3. Reports the current throttle level (0=normal..3=emergency).

The throttle level itself is consulted by callers to decide how
aggressively to back off. The design doc is at
``docs/STORAGE_BUDGET_DESIGN.md`` §6.

This module is small on purpose: it MUST not be a hot-path dependency
that adds latency to each capture. The DB writes happen at most once a
minute (the throttle decision is cached for ~60 s) and the read path
returns from in-memory state when fresh.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from typing import Literal

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.budget")

ThrottleLevel = Literal[0, 1, 2, 3]
Bucket = Literal[
    "thumbnails",
    "audio",
    "events",
    "ocr_text",
    "embeddings",
    "misc",
]

_COLUMN_FOR_BUCKET: dict[Bucket, str] = {
    "thumbnails": "thumbnails_bytes",
    "audio": "audio_bytes",
    "events": "events_bytes",
    "ocr_text": "ocr_text_bytes",
    "embeddings": "embeddings_bytes",
    "misc": "misc_bytes",
}

_CACHED_LEVEL: ThrottleLevel = 0
_CACHED_UNTIL: datetime | None = None
_LEVEL_LOCK = asyncio.Lock()
_CACHE_TTL_SECONDS = 60.0


def _today_utc() -> str:
    return date.today().isoformat()


async def add_bytes(bucket: Bucket, n: int) -> None:
    """Record ``n`` bytes written into ``bucket`` for today."""
    if n <= 0:
        return
    column = _COLUMN_FOR_BUCKET[bucket]
    day = _today_utc()
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO daily_budget_state (day) VALUES (?) "
            "ON CONFLICT(day) DO NOTHING",
            (day,),
        )
        await conn.execute(
            f"UPDATE daily_budget_state SET {column} = {column} + ?, "  # noqa: S608
            "last_updated = datetime('now') WHERE day = ?",
            (int(n), day),
        )
        await conn.commit()


async def get_today_bytes() -> dict[Bucket, int]:
    """Return today's per-bucket byte totals. Zero for buckets untouched today."""
    day = _today_utc()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT thumbnails_bytes, audio_bytes, events_bytes, "
            "ocr_text_bytes, embeddings_bytes, misc_bytes "
            "FROM daily_budget_state WHERE day = ?",
            (day,),
        )
        row = await cursor.fetchone()
    if row is None:
        return {bucket: 0 for bucket in _COLUMN_FOR_BUCKET}
    return {
        "thumbnails": int(row["thumbnails_bytes"]),
        "audio": int(row["audio_bytes"]),
        "events": int(row["events_bytes"]),
        "ocr_text": int(row["ocr_text_bytes"]),
        "embeddings": int(row["embeddings_bytes"]),
        "misc": int(row["misc_bytes"]),
    }


async def _project_eod() -> int:
    """Return today's projected end-of-day usage in bytes."""
    today_bytes = sum((await get_today_bytes()).values())
    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_elapsed = max((now - midnight).total_seconds() / 3600.0, 1.0)
    # Floor the scaling factor at 1.0 — early-morning ticks would
    # otherwise extrapolate a few KB to gigabytes.
    return int(today_bytes / hours_elapsed * 24.0)


async def _compute_level() -> ThrottleLevel:
    """Recompute throttle level from current state. Bypasses the cache."""
    cfg = get_settings()
    if not cfg.budget_enforcer_enabled:
        return 0
    cap_bytes = int(cfg.daily_budget_mb * 1024 * 1024)
    today_total = sum((await get_today_bytes()).values())
    projected = await _project_eod()
    if today_total >= cap_bytes:
        return 3
    if projected >= int(cap_bytes * 1.30):
        return 3
    if projected >= cap_bytes:
        return 2
    if projected >= int(cap_bytes * 0.90):
        return 1
    return 0


async def get_throttle_level() -> ThrottleLevel:
    """Return the current throttle level, cached for ~60 s.

    Hot-path callers (capture loop, audio worker) should consult this
    rather than recomputing per-iteration.
    """
    global _CACHED_LEVEL, _CACHED_UNTIL  # noqa: PLW0603
    now = datetime.now(UTC)
    if _CACHED_UNTIL is not None and now < _CACHED_UNTIL:
        return _CACHED_LEVEL
    async with _LEVEL_LOCK:
        # Re-check inside the lock; a peer may have just refreshed.
        if _CACHED_UNTIL is not None and datetime.now(UTC) < _CACHED_UNTIL:
            return _CACHED_LEVEL
        level = await _compute_level()
        _CACHED_LEVEL = level
        _CACHED_UNTIL = datetime.now(UTC).replace(microsecond=0)
        # Persist the level so the UI can render it.
        try:
            day = _today_utc()
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE daily_budget_state SET throttle_level = ? "
                    "WHERE day = ?",
                    (int(level), day),
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("budget.persist_level_failed", error=str(exc))
        from datetime import timedelta  # noqa: PLC0415

        _CACHED_UNTIL = _CACHED_UNTIL + timedelta(seconds=_CACHE_TTL_SECONDS)
        return level


def invalidate_cache() -> None:
    """Drop the cached throttle level. Tests + settings updates call this."""
    global _CACHED_UNTIL  # noqa: PLW0603
    _CACHED_UNTIL = None
