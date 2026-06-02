"""Per-day TL;DR auto-summary — one-sentence cached recap of a calendar day.

This is the third tier of LLM digests in Persona:
  - daily   (v0.13) — 120-200 word first-person summary (``summarise_day``)
  - weekly  (v0.22) — 250-400 word Mon-Sun retrospective (``summarise_week``)
  - tldr    (v0.36) — ONE sentence (<=30 words), surfaced inline on
                      /timeline/{day} pages and the /digest list.

The TL;DR is generated lazily on first request and cached in ``day_tldr``.
Callers MUST NOT block the render path on this — the route layer exposes it
via an async JSON endpoint that the client fetches after page paint.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from re import Pattern
from re import compile as re_compile
from typing import TYPE_CHECKING, Literal, TypedDict

from app.llm.client import CompletionRequest, LLMClient, LLMNotConfigured, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.day_tldr")

Status = Literal["ok", "empty", "missing_config"]


class DayTldrResult(TypedDict):
    status: Status
    tldr: str
    cached: bool


_SYSTEM = (
    "You are a memory assistant for a single user. You receive a compact "
    "summary of ONE calendar day: top apps used, capture counts, and salient "
    "OCR keywords. Produce EXACTLY ONE sentence (max 30 words) capturing the "
    "theme of the day in first person. No headings, no lists, no preamble. "
    "Write it in the user's language (Russian if Cyrillic dominates the "
    "source text, English otherwise). Do NOT invent facts. If nothing "
    "meaningful was captured, reply with one short honest sentence."
)

# Crude keyword extractor: alphabetic runs (Latin + Cyrillic) of length >= 4.
# Used to surface OCR vocabulary to the LLM without shipping raw text.
_WORD_RE: Pattern[str] = re_compile(r"[A-Za-zЀ-ӿ]{4,}")

# Generic UI chrome words we don't want polluting the keyword list.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "https",
        "http",
        "www",
        "com",
        "org",
        "html",
        "true",
        "false",
        "none",
        "null",
        "this",
        "that",
        "with",
        "from",
        "have",
        "your",
        "about",
        "click",
        "menu",
        "open",
        "close",
        "save",
        "edit",
        "view",
        "file",
        "settings",
        "back",
        "next",
    }
)


def _parse_day(day_iso: str) -> date:
    """Validate and parse a YYYY-MM-DD string. Raises ValueError on bad input."""
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


def _keyword_counter(texts: list[str]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for text in texts:
        for raw in _WORD_RE.findall(text):
            word = raw.lower()
            if word in _STOPWORDS:
                continue
            counter[word] += 1
    return counter


def _build_user_prompt(
    *,
    day_iso: str,
    total: int,
    apps: list[tuple[str, int]],
    keywords: list[tuple[str, int]],
) -> str:
    apps_line = (
        ", ".join(f"{name} ({count})" for name, count in apps) if apps else "—"
    )
    kw_line = (
        ", ".join(f"{word} ({count})" for word, count in keywords) if keywords else "—"
    )
    return (
        f"Day: {day_iso}\n"
        f"Total captures: {total}\n"
        f"Top apps: {apps_line}\n"
        f"OCR keywords: {kw_line}\n\n"
        "Write the one-sentence TL;DR now."
    )


async def _read_cached(conn: aiosqlite.Connection, day_iso: str) -> str | None:
    cursor = await conn.execute(
        "SELECT tldr FROM day_tldr WHERE day = ?",
        (day_iso,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["tldr"])


async def _write_cached(
    conn: aiosqlite.Connection,
    *,
    day_iso: str,
    tldr: str,
    provider: str | None,
) -> None:
    await conn.execute(
        "INSERT INTO day_tldr (day, tldr, provider, generated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day) DO UPDATE SET "
        "tldr = excluded.tldr, "
        "provider = excluded.provider, "
        "generated_at = excluded.generated_at",
        (day_iso, tldr, provider, iso(datetime.now(UTC))),
    )
    await conn.commit()


async def _gather_day_signal(
    conn: aiosqlite.Connection, day_iso: str
) -> tuple[int, list[tuple[str, int]], list[tuple[str, int]]]:
    """Pull lightweight per-day signal: total captures, top apps, top OCR words."""
    target = _parse_day(day_iso)
    since = datetime.combine(target, time.min, tzinfo=UTC)
    until = since + timedelta(days=1)
    since_iso, until_iso = iso(since), iso(until)

    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ?",
        (since_iso, until_iso),
    )
    total_row = await cursor.fetchone()
    total = int(total_row["n"]) if total_row else 0

    cursor = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL "
        "GROUP BY app_name ORDER BY n DESC LIMIT 6",
        (since_iso, until_iso),
    )
    app_rows = await cursor.fetchall()
    apps: list[tuple[str, int]] = [
        (str(row["app_name"]), int(row["n"])) for row in app_rows
    ]

    cursor = await conn.execute(
        "SELECT ocr_text FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "AND ocr_text IS NOT NULL AND length(ocr_text) > 0 "
        "ORDER BY captured_at ASC LIMIT 300",
        (since_iso, until_iso),
    )
    ocr_rows = await cursor.fetchall()
    texts: list[str] = [str(row["ocr_text"]) for row in ocr_rows]
    keywords = _keyword_counter(texts).most_common(12)

    return total, apps, keywords


async def summarise_day_tldr(
    day_iso: str,
    *,
    client: LLMClient | None = None,
    force: bool = False,
) -> DayTldrResult:
    """Return a cached or freshly generated one-sentence TL;DR for ``day_iso``.

    Args:
        day_iso: Calendar day in ``YYYY-MM-DD`` form (raises ValueError on bad).
        client: Optional preconstructed LLM client (mainly for tests).
        force: If True, ignore any cached row and regenerate.

    Returns:
        ``DayTldrResult`` with:
          - ``status``: ``ok`` (have a tldr), ``empty`` (no captures), or
            ``missing_config`` (no LLM configured and no cached row).
          - ``tldr``: the sentence text (empty string when status != ok).
          - ``cached``: True if served from cache.
    """
    parsed_day = _parse_day(day_iso)
    canonical = parsed_day.isoformat()

    async with get_connection() as conn:
        if not force:
            cached = await _read_cached(conn, canonical)
            if cached is not None:
                log.info("day_tldr.cache.hit", day=canonical)
                return {"status": "ok", "tldr": cached, "cached": True}

        total, apps, keywords = await _gather_day_signal(conn, canonical)

        if total == 0:
            log.info("day_tldr.empty", day=canonical)
            return {"status": "empty", "tldr": "", "cached": False}

        try:
            ll = client or make_client()
        except LLMNotConfigured:
            log.info("day_tldr.missing_config", day=canonical)
            return {"status": "missing_config", "tldr": "", "cached": False}

        user_message = _build_user_prompt(
            day_iso=canonical, total=total, apps=apps, keywords=keywords
        )
        request = CompletionRequest(
            system=_SYSTEM,
            user=user_message,
            max_tokens=120,
            temperature=0.3,
        )

        log.info(
            "day_tldr.generate.start",
            day=canonical,
            total=total,
            top_apps=len(apps),
            keywords=len(keywords),
            provider=ll.provider,
        )
        text = (await ll.complete(request)).strip()
        if not text:
            log.warning("day_tldr.generate.empty_response", day=canonical)
            return {"status": "empty", "tldr": "", "cached": False}

        await _write_cached(
            conn, day_iso=canonical, tldr=text, provider=ll.provider
        )
        log.info(
            "day_tldr.generate.done",
            day=canonical,
            provider=ll.provider,
            chars=len(text),
        )
        return {"status": "ok", "tldr": text, "cached": False}
