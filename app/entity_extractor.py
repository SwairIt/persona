"""Cross-day fact extraction (v1.27).

Heuristic-first entity discovery over the hourly_card stream. Names
(people, projects, topics) mentioned three or more times accumulate
into a permanent ledger so the operator can ask "who/what did I work
on this month?" long after the raw screens/audio are gone.

Pipeline
--------
1. :func:`extract_from_text` scans the input string for capitalised
   tokens (sentence-start words are demoted because they're noise:
   "Today I met X" → "Today" is not an entity). Each candidate is
   classified as person/topic/project/other via cheap heuristics:

   * 1-2 word run where each word starts with an upper-case letter →
     ``person`` ("Denis Pavlov", "Anna").
   * Otherwise → ``topic`` (single capitalised noun, or 3+ word run).

   A deny-list filters the usual English/Russian false positives
   (weekday + month names, common interjections).

2. :func:`ingest_mentions_from_hourly_cards` walks hourly_card rows
   newer than the ``entity_last_card_processed`` kv watermark, runs
   the extractor over ``summary + transcript_excerpt``, and upserts
   into ``entity`` (mention_count += 1, last_seen = now) plus inserts
   one ``entity_mention`` row per hit. The watermark advances after a
   successful pass so the worker is exactly-once on restart.

3. :func:`get_top_entities` is the public read API for the /entities
   page and the cross-day RAG retriever.

Everything is parametrised SQL; no f-string SQL anywhere.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.entity_extractor")


_WATERMARK_KV: Final[str] = "entity_last_card_processed"
"""kv_settings key holding the max hourly_card.hour_start consumed."""


# Cyrillic A/a look identical to Latin A/a and trip ruff's RUF001 ambiguous-
# character rule. We *want* the Cyrillic range here (Russian names must
# match), so the noqa is targeted — not a blanket file-level suppression.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"\b([A-ZА-ЯЁ][A-Za-zА-Яа-яЁё\-]{1,29}"  # noqa: RUF001
    r"(?:\s+[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё\-]{1,29}){0,2})\b",  # noqa: RUF001
)
"""Match a run of 1-3 capitalised words (latin or cyrillic).

Each segment is 2-30 chars per the spec. The whole match becomes one
candidate; ``extract_from_text`` then splits on whitespace to decide
person vs topic.
"""


_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?\n])\s+")
"""Coarse sentence boundary — good enough to detect sentence-start.

We do not need a full NLP tokenizer here; we only need to know which
candidates sit at offset 0 of a sentence (and are therefore demoted
from "real entity" to "could be the first word of a sentence").
"""


_DENY: Final[frozenset[str]] = frozenset(
    {
        # Weekdays — English
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday",
        # Months — English
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
        # Common single-word false positives — English
        "ok", "okay", "yes", "no", "today", "tomorrow", "yesterday",
        "tonight", "morning", "evening", "afternoon", "night",
        "hello", "hi", "hey", "thanks", "thank you", "please",
        "the", "and", "or", "but", "if", "so", "well", "right",
        "google", "microsoft",  # too generic to be a personal entity
        # Russian weekdays / months / fillers (lowercased)
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
        "январь", "февраль", "март", "апрель", "май", "июнь",
        "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        "да", "нет", "сегодня", "завтра", "вчера",
        "привет", "пока", "спасибо", "пожалуйста",
    }
)
"""Lower-cased deny-list of names that look capitalised but aren't entities."""


_MIN_LEN: Final[int] = 2
_MAX_LEN: Final[int] = 30


def _classify(name: str) -> str:
    """Return ``person`` for 1-2 word title-cased names, else ``topic``.

    "Denis"          → person
    "Denis Pavlov"   → person
    "Postgres"       → topic
    "Open AI Codex"  → topic (3 words)
    """
    parts = name.split()
    if 1 <= len(parts) <= 2 and all(
        p and p[0].isupper() and p[1:].islower() if len(p) > 1 else p[0].isupper()
        for p in parts
    ):
        return "person"
    return "topic"


def _sentence_start_offsets(text: str) -> set[int]:
    """Return character offsets that start a sentence.

    A token sitting at one of these offsets is demoted from a real
    entity candidate (e.g. "Today I" — "Today" is sentence-start).
    """
    offsets: set[int] = {0}
    for match in _SENTENCE_SPLIT_RE.finditer(text):
        offsets.add(match.end())
    return offsets


async def extract_from_text(text: str) -> list[dict[str, str]]:
    """Pull entity candidates from ``text`` heuristically.

    Returns a list of ``{"name": ..., "kind": ...}`` dicts, deduplicated
    by ``(name, kind)`` while preserving first-seen order. Empty input
    returns an empty list.
    """
    if not text:
        return []

    sentence_starts = _sentence_start_offsets(text)
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []

    for match in _TOKEN_RE.finditer(text):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        if match.start() in sentence_starts:
            # Demote sentence-start candidates — too noisy to trust.
            continue
        # Length guard applies to the whole candidate (incl. spaces).
        if not (_MIN_LEN <= len(candidate) <= _MAX_LEN * 3 + 2):
            continue
        if candidate.lower() in _DENY:
            continue

        kind = _classify(candidate)
        key = (candidate, kind)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": candidate, "kind": kind})

    return out


async def _upsert_entity(
    conn: aiosqlite.Connection,
    *,
    name: str,
    kind: str,
    mentioned_at: str,
    source_kind: str,
    source_id: int | None,
) -> int:
    """Upsert an entity row and append one mention. Returns ``entity.id``."""
    await conn.execute(
        """
        INSERT INTO entity (name, kind, first_seen, last_seen, mention_count)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(name, kind) DO UPDATE SET
            mention_count = mention_count + 1,
            last_seen = excluded.last_seen
        """,
        (name, kind, mentioned_at, mentioned_at),
    )
    cursor = await conn.execute(
        "SELECT id FROM entity WHERE name = ? AND kind = ?",
        (name, kind),
    )
    row = await cursor.fetchone()
    if row is None:  # pragma: no cover — we just inserted it
        raise RuntimeError("entity upsert produced no row")
    entity_id = int(row["id"])
    await conn.execute(
        """
        INSERT INTO entity_mention (entity_id, source_kind, source_id, mentioned_at)
        VALUES (?, ?, ?, ?)
        """,
        (entity_id, source_kind, source_id, mentioned_at),
    )
    return entity_id


async def ingest_mentions_from_hourly_cards(days: int = 7) -> dict[str, int]:
    """Sweep recent hourly_card rows and update the entity ledger.

    Walks every hourly_card with ``hour_start`` newer than the watermark
    kv (``entity_last_card_processed``), at most ``days`` days back on
    the first run. After ingestion, advances the watermark to the
    largest ``hour_start`` consumed so the next tick is a no-op.

    Returns a counter dict with ``cards``, ``entities``, ``mentions``.
    """
    cutoff_iso = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()
    stats: dict[str, int] = {"cards": 0, "entities": 0, "mentions": 0}

    async with get_connection() as conn:
        watermark = await get_kv(conn, _WATERMARK_KV)
        floor = watermark or cutoff_iso

        cursor = await conn.execute(
            """
            SELECT rowid, hour_start, summary, transcript_excerpt
            FROM hourly_card
            WHERE hour_start > ?
            ORDER BY hour_start ASC
            """,
            (floor,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return stats

        max_hour: str = floor
        for r in rows:
            stats["cards"] += 1
            text = " ".join(
                str(r[col] or "") for col in ("summary", "transcript_excerpt")
            )
            hits = await extract_from_text(text)
            for hit in hits:
                await _upsert_entity(
                    conn,
                    name=hit["name"],
                    kind=hit["kind"],
                    mentioned_at=str(r["hour_start"]),
                    source_kind="hourly_card",
                    source_id=int(r["rowid"]),
                )
                stats["mentions"] += 1
            if hits:
                stats["entities"] += len({(h["name"], h["kind"]) for h in hits})
            max_hour = max(max_hour, str(r["hour_start"]))

        if max_hour != floor:
            await set_kv(conn, _WATERMARK_KV, max_hour)
        await conn.commit()

    log.info(
        "entity_extractor.cycle",
        cards=stats["cards"],
        entities=stats["entities"],
        mentions=stats["mentions"],
        watermark=max_hour,
    )
    return stats


async def get_top_entities(
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, object]]:
    """Return top entities ordered by ``mention_count`` DESC.

    Pass ``kind`` to filter to one bucket (``person``/``project``/
    ``topic``/``other``); ``None`` returns the global top-N across all
    kinds. Each row is a plain dict ready for JSON serialisation.
    """
    async with get_connection() as conn:
        if kind is None:
            cursor = await conn.execute(
                """
                SELECT id, name, kind, first_seen, last_seen, mention_count
                FROM entity
                ORDER BY mention_count DESC, last_seen DESC
                LIMIT ?
                """,
                (int(limit),),
            )
        else:
            cursor = await conn.execute(
                """
                SELECT id, name, kind, first_seen, last_seen, mention_count
                FROM entity
                WHERE kind = ?
                ORDER BY mention_count DESC, last_seen DESC
                LIMIT ?
                """,
                (kind, int(limit)),
            )
        rows = await cursor.fetchall()

    return [
        {
            "id": int(r["id"]),
            "name": str(r["name"]),
            "kind": str(r["kind"]),
            "first_seen": str(r["first_seen"]),
            "last_seen": str(r["last_seen"]),
            "mention_count": int(r["mention_count"]),
        }
        for r in rows
    ]


async def get_entity(entity_id: int) -> dict[str, object] | None:
    """Return one entity row by id, or ``None`` if it does not exist."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, name, kind, first_seen, last_seen, mention_count
            FROM entity WHERE id = ?
            """,
            (int(entity_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "kind": str(row["kind"]),
        "first_seen": str(row["first_seen"]),
        "last_seen": str(row["last_seen"]),
        "mention_count": int(row["mention_count"]),
    }


async def get_entity_timeline(
    entity_id: int,
    limit: int = 200,
) -> list[dict[str, object]]:
    """Return mention rows for ``entity_id`` joined with hourly_card.

    Only ``hourly_card`` sources are joined right now — that's the only
    producer; other producers will simply show their ``source_kind``
    label without a card preview.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT
                m.id AS mention_id,
                m.source_kind AS source_kind,
                m.source_id AS source_id,
                m.mentioned_at AS mentioned_at,
                c.hour_start AS hour_start,
                c.summary AS card_summary
            FROM entity_mention m
            LEFT JOIN hourly_card c
                ON m.source_kind = 'hourly_card' AND c.rowid = m.source_id
            WHERE m.entity_id = ?
            ORDER BY m.mentioned_at DESC
            LIMIT ?
            """,
            (int(entity_id), int(limit)),
        )
        rows = await cursor.fetchall()

    return [
        {
            "mention_id": int(r["mention_id"]),
            "source_kind": str(r["source_kind"]),
            "source_id": int(r["source_id"]) if r["source_id"] is not None else None,
            "mentioned_at": str(r["mentioned_at"]),
            "hour_start": str(r["hour_start"]) if r["hour_start"] else None,
            "card_summary": str(r["card_summary"]) if r["card_summary"] else None,
        }
        for r in rows
    ]


__all__ = [
    "extract_from_text",
    "get_entity",
    "get_entity_timeline",
    "get_top_entities",
    "ingest_mentions_from_hourly_cards",
]
