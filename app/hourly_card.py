"""Tier 1 memory: build one summary card per completed hour (v1.14).

The card holds enough to answer "what was I doing at 14:00 yesterday?"
without scanning thousands of raw screenshot rows. It is computed
DETERMINISTICALLY first — apps used, screen count, audio length, top
OCR words, first 500 chars of transcript. LLM narrative enrichment is
an opt-in pass that runs on top.

The card is written to a small dedicated FTS5-mirrored table so the
``/ask`` endpoint can search across hours in O(log n).

This module is import-safe: it never touches the network, the LLM, or
the audio worker. It only reads from existing tables.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.hourly_card")


_STOP = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "while", "for", "to",
        "from", "of", "in", "on", "at", "with", "by", "as", "is", "it",
        "this", "that", "these", "those", "be", "are", "was", "were",
        "не", "и", "в", "на", "с", "по", "что", "как", "это", "так",
        "у", "за", "о", "до", "из", "к", "то", "же", "бы", "ли", "но",
    }
)
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}", re.UNICODE)


def _hour_bucket(when: datetime) -> tuple[datetime, datetime]:
    """Return ``(hour_start, hour_end)`` for the hour containing ``when``."""
    floor = when.replace(minute=0, second=0, microsecond=0)
    return floor, floor + timedelta(hours=1) - timedelta(seconds=1)


async def _gather_apps(
    conn: aiosqlite.Connection,
    start: datetime,
    end: datetime,
) -> list[dict[str, int | str]]:
    """Top apps by capture count in the hour. Capture count ≈ time-spent proxy."""
    cursor = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE app_name IS NOT NULL AND app_name != '' "
        "AND captured_at >= ? AND captured_at <= ? "
        "GROUP BY app_name ORDER BY n DESC LIMIT 5",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cursor.fetchall()
    return [{"app": str(r["app_name"]), "shots": int(r["n"])} for r in rows]


async def _gather_top_words(
    conn: aiosqlite.Connection,
    start: datetime,
    end: datetime,
) -> list[str]:
    """Top OCR words in the hour, stop-words filtered."""
    cursor = await conn.execute(
        "SELECT ocr_text FROM screenshots "
        "WHERE ocr_text IS NOT NULL AND ocr_text != '' "
        "AND captured_at >= ? AND captured_at <= ?",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cursor.fetchall()
    counter: Counter[str] = Counter()
    for r in rows:
        text = str(r["ocr_text"] or "")
        for match in _WORD_RE.findall(text):
            w = match.lower()
            if w in _STOP or len(w) > 24:
                continue
            counter[w] += 1
    return [w for w, _ in counter.most_common(15)]


async def _gather_audio(
    conn: aiosqlite.Connection,
    start: datetime,
    end: datetime,
) -> tuple[int, str]:
    """Total voiced seconds + first 500 chars of transcript in the hour."""
    cursor = await conn.execute(
        "SELECT COALESCE(SUM(duration_seconds), 0) AS dur, "
        "GROUP_CONCAT(COALESCE(transcript, ''), ' ') AS text "
        "FROM audio_segment "
        "WHERE captured_at >= ? AND captured_at <= ?",
        (start.isoformat(), end.isoformat()),
    )
    row = await cursor.fetchone()
    duration = int(row["dur"] or 0) if row else 0
    excerpt = (str(row["text"] or "") if row else "")[:500].strip()
    return duration, excerpt


def _render_markdown(
    *,
    hour_start: datetime,
    apps: list[dict[str, int | str]],
    screen_count: int,
    audio_seconds: int,
    top_words: list[str],
    transcript: str,
) -> str:
    """Render a small markdown block describing the hour."""
    lines = [
        f"## {hour_start.strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    if apps:
        app_strs = ", ".join(f"**{a['app']}** ({a['shots']})" for a in apps)
        lines.append(f"- Apps: {app_strs}")
    lines.append(f"- Screens: {screen_count}")
    if audio_seconds:
        lines.append(f"- Voice: {audio_seconds // 60}m {audio_seconds % 60}s")
    if top_words:
        lines.append(f"- Keywords: {', '.join(top_words[:10])}")
    if transcript:
        lines.append("")
        lines.append(f"> {transcript}")
    return "\n".join(lines)


async def build_card_for_hour(
    when: datetime | None = None,
    *,
    upsert: bool = True,
) -> dict[str, object] | None:
    """Compute one card for the hour containing ``when`` (default = last hour).

    Returns the inserted row as a dict, or ``None`` if there was no
    activity in that hour (no screens AND no audio segments — we don't
    write empty cards).
    """
    now = when or (datetime.now(tz=UTC) - timedelta(hours=1))
    start, end = _hour_bucket(now)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at <= ?",
            (start.isoformat(), end.isoformat()),
        )
        row = await cursor.fetchone()
        screen_count = int(row["n"]) if row else 0
        audio_seconds, transcript = await _gather_audio(conn, start, end)

        if screen_count == 0 and audio_seconds == 0:
            log.debug("hourly_card.skipped_empty", hour=start.isoformat())
            return None

        apps = await _gather_apps(conn, start, end)
        top_words = await _gather_top_words(conn, start, end)
        summary = _render_markdown(
            hour_start=start,
            apps=apps,
            screen_count=screen_count,
            audio_seconds=audio_seconds,
            top_words=top_words,
            transcript=transcript,
        )

        if upsert:
            await conn.execute(
                "INSERT OR REPLACE INTO hourly_card "
                "(hour_start, hour_end, summary, apps_json, screen_count, "
                " audio_seconds, top_words, transcript_excerpt, llm_enriched) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    start.isoformat(),
                    end.isoformat(),
                    summary,
                    json.dumps(apps, ensure_ascii=False),
                    screen_count,
                    audio_seconds,
                    ", ".join(top_words),
                    transcript,
                ),
            )
            await conn.commit()

    log.info(
        "hourly_card.built",
        hour=start.isoformat(),
        screens=screen_count,
        audio_s=audio_seconds,
        apps=len(apps),
        keywords=len(top_words),
    )
    return {
        "hour_start": start.isoformat(),
        "hour_end": end.isoformat(),
        "summary": summary,
        "screen_count": screen_count,
        "audio_seconds": audio_seconds,
        "apps": apps,
        "top_words": top_words,
    }


async def list_recent_cards(limit: int = 24) -> list[dict[str, object]]:
    """Return the most recent N cards (newest first), used by the dashboard."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT hour_start, hour_end, summary, screen_count, audio_seconds, "
            "       apps_json, top_words FROM hourly_card "
            "ORDER BY hour_start DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cursor.fetchall()
    return [
        {
            "hour_start": str(r["hour_start"]),
            "hour_end": str(r["hour_end"]),
            "summary": str(r["summary"]),
            "screen_count": int(r["screen_count"] or 0),
            "audio_seconds": int(r["audio_seconds"] or 0),
            "apps": json.loads(r["apps_json"]) if r["apps_json"] else [],
            "top_words": str(r["top_words"] or ""),
        }
        for r in rows
    ]
