"""Per-app day digest — one-sentence LLM recap of activity in a single app.

This sits next to :mod:`app.llm.day_tldr` (v0.36 whole-day TL;DR) and uses
the same BYO LLM plumbing. The difference is the granularity:

  - ``day_tldr``         — one sentence for the *entire* day across all apps.
  - ``per_app_digest``   — one sentence per ``app_name`` for a single day.

The page at ``/digest/apps?day=YYYY-MM-DD`` renders a table with one row
per app the user touched that day. Each row is generated lazily on first
view and cached in ``app_day_digest`` so a reload does not spend tokens.

Callers MUST NOT block render on this — the route layer fetches each row
asynchronously after page paint, identical to how ``day_tldr`` is wired.
"""

from __future__ import annotations

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

log = get_logger("persona.per_app_digest")

Status = Literal["ok", "empty", "missing_config"]


class AppDigestResult(TypedDict):
    status: Status
    tldr: str


# Cap how much OCR text we ship to the LLM per app. Anything beyond this is
# noise for a one-sentence summary and only inflates the prompt + cost.
_MAX_OCR_ROWS = 80
_MAX_OCR_CHARS_PER_ROW = 400
_MAX_OCR_TOTAL_CHARS = 8000

# Strip control characters and collapse runs of whitespace before feeding
# OCR text to the LLM. OCR output frequently contains stray \x00 / \r
# fragments that waste tokens and confuse model output.
_WHITESPACE_RE: Pattern[str] = re_compile(r"\s+")
_CONTROL_RE: Pattern[str] = re_compile(r"[\x00-\x08\x0b-\x1f\x7f]")


_SYSTEM = (
    "You are a memory assistant for a single user. You receive raw OCR "
    "snippets captured from ONE application on ONE calendar day. Produce "
    "EXACTLY ONE sentence (max 30 words) describing what the user did in "
    "that app on that day, in first person. No headings, no lists, no "
    "preamble, no quotes around the sentence. Write it in the user's "
    "language (Russian if Cyrillic dominates the source text, English "
    "otherwise). Do NOT invent facts. If the snippets are mostly chrome "
    "or unreadable, reply with one short honest sentence saying so."
)


def _parse_day(day_iso: str) -> date:
    """Validate and parse a YYYY-MM-DD string. Raises ValueError on bad input."""
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


def _clean_snippet(text: str) -> str:
    """Strip control chars, collapse whitespace, clip to a sane row length."""
    no_ctrl = _CONTROL_RE.sub(" ", text)
    collapsed = _WHITESPACE_RE.sub(" ", no_ctrl).strip()
    if len(collapsed) > _MAX_OCR_CHARS_PER_ROW:
        return collapsed[:_MAX_OCR_CHARS_PER_ROW].rstrip() + "…"
    return collapsed


def _build_user_prompt(
    *,
    day_iso: str,
    app_name: str,
    total: int,
    snippets: list[str],
) -> str:
    """Render the user message: header + bulleted OCR snippets, length-clipped."""
    bullets: list[str] = []
    used = 0
    for snippet in snippets:
        if not snippet:
            continue
        line = f"- {snippet}"
        if used + len(line) + 1 > _MAX_OCR_TOTAL_CHARS:
            break
        bullets.append(line)
        used += len(line) + 1
    body = "\n".join(bullets) if bullets else "(no readable OCR text)"
    return (
        f"Day: {day_iso}\n"
        f"App: {app_name}\n"
        f"Captures in this app: {total}\n"
        f"OCR snippets (most recent first):\n{body}\n\n"
        "Write the one-sentence summary now."
    )


async def _read_cached(
    conn: aiosqlite.Connection, *, day_iso: str, app_name: str
) -> str | None:
    cursor = await conn.execute(
        "SELECT tldr FROM app_day_digest WHERE day = ? AND app_name = ?",
        (day_iso, app_name),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["tldr"])


async def _write_cached(
    conn: aiosqlite.Connection,
    *,
    day_iso: str,
    app_name: str,
    tldr: str,
) -> None:
    await conn.execute(
        "INSERT INTO app_day_digest (day, app_name, tldr, generated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(day, app_name) DO UPDATE SET "
        "tldr = excluded.tldr, "
        "generated_at = excluded.generated_at",
        (day_iso, app_name, tldr, iso(datetime.now(UTC))),
    )
    await conn.commit()


async def _gather_app_signal(
    conn: aiosqlite.Connection,
    *,
    day_iso: str,
    app_name: str,
) -> tuple[int, list[str]]:
    """Pull capture count + cleaned OCR snippets for one app on one day."""
    target = _parse_day(day_iso)
    since = datetime.combine(target, time.min, tzinfo=UTC)
    until = since + timedelta(days=1)
    since_iso, until_iso = iso(since), iso(until)

    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name = ?",
        (since_iso, until_iso, app_name),
    )
    total_row = await cursor.fetchone()
    total = int(total_row["n"]) if total_row else 0

    cursor = await conn.execute(
        "SELECT ocr_text FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name = ? "
        "AND ocr_text IS NOT NULL AND length(ocr_text) > 0 "
        "ORDER BY captured_at DESC LIMIT ?",
        (since_iso, until_iso, app_name, _MAX_OCR_ROWS),
    )
    rows = await cursor.fetchall()
    snippets: list[str] = []
    for row in rows:
        cleaned = _clean_snippet(str(row["ocr_text"]))
        if cleaned:
            snippets.append(cleaned)
    return total, snippets


async def summarise_app_day(
    day: str,
    app_name: str,
    *,
    client: LLMClient | None = None,
    force: bool = False,
) -> AppDigestResult:
    """Return a cached or freshly generated one-sentence digest for ``(day, app)``.

    Args:
        day: Calendar day in ``YYYY-MM-DD`` form (raises ValueError on bad).
        app_name: Exact ``screenshots.app_name`` value to summarise.
        client: Optional preconstructed LLM client (mainly for tests).
        force: If True, ignore any cached row and regenerate.

    Returns:
        ``AppDigestResult`` with:
          - ``status``: ``ok`` (have a tldr), ``empty`` (no captures for that
            app on that day), or ``missing_config`` (no LLM configured and
            no cached row to fall back on).
          - ``tldr``: the sentence text (empty string when status != ok).
    """
    parsed_day = _parse_day(day)
    canonical = parsed_day.isoformat()
    name = app_name.strip()
    if not name:
        log.info("per_app_digest.empty.no_app", day=canonical)
        return {"status": "empty", "tldr": ""}

    async with get_connection() as conn:
        if not force:
            cached = await _read_cached(conn, day_iso=canonical, app_name=name)
            if cached is not None:
                log.info(
                    "per_app_digest.cache.hit",
                    day=canonical,
                    app_name=name,
                )
                return {"status": "ok", "tldr": cached}

        total, snippets = await _gather_app_signal(
            conn, day_iso=canonical, app_name=name
        )

        if total == 0:
            log.info(
                "per_app_digest.empty",
                day=canonical,
                app_name=name,
            )
            return {"status": "empty", "tldr": ""}

        try:
            ll = client or make_client()
        except LLMNotConfigured:
            log.info(
                "per_app_digest.missing_config",
                day=canonical,
                app_name=name,
            )
            return {"status": "missing_config", "tldr": ""}

        user_message = _build_user_prompt(
            day_iso=canonical,
            app_name=name,
            total=total,
            snippets=snippets,
        )
        request = CompletionRequest(
            system=_SYSTEM,
            user=user_message,
            max_tokens=120,
            temperature=0.3,
        )

        log.info(
            "per_app_digest.generate.start",
            day=canonical,
            app_name=name,
            total=total,
            snippets=len(snippets),
            provider=ll.provider,
        )
        text = (await ll.complete(request)).strip()
        if not text:
            log.warning(
                "per_app_digest.generate.empty_response",
                day=canonical,
                app_name=name,
            )
            return {"status": "empty", "tldr": ""}

        await _write_cached(
            conn, day_iso=canonical, app_name=name, tldr=text
        )
        log.info(
            "per_app_digest.generate.done",
            day=canonical,
            app_name=name,
            provider=ll.provider,
            chars=len(text),
        )
        return {"status": "ok", "tldr": text}
