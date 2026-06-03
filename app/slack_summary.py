"""Slack-style daily summary — a compact, one-emoji-per-bullet recap.

Designed for pasting into Slack / Mattermost / Discord without any LLM
in the loop.  The output is plain text with light Markdown — Slack
renders the leading triple-backtick header as a fixed-width block, and
bullets show up as bullets.  The function is read-only: it joins three
existing data sources without writing anything.

Sections
--------
* **Header** — a fixed-width block (triple-backtick fence) carrying the
  day, the product name, the total shot count and total minutes spent.
* **Per-app bullets** — top 3 apps with hours/minutes; one camera-style
  emoji per bullet keeps the chat compact ("one-emoji-per-bullet" is
  the spec, not three).
* **Keywords line** — the top 5 OCR keywords for the day on a single
  trailing line so the message stays small in the channel.

The whole thing is pure Python — no LLM, no network, no template
engine.  The output is a single ``str`` so the route can encode it once
and the CLI can ``Path.write_text`` it in one shot.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Final

from app.keywords import STOPWORDS
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.time_on_app import daily_time_on_app

log = get_logger("persona.slack_summary")

# Slack-friendly bullet glyphs — one per section so a quick eye-scan
# tells you what each line is about without parsing the words.
_HEADER_EMOJI: Final[str] = ":calendar:"
_APP_EMOJI: Final[str] = ":camera_with_flash:"
_KEYWORDS_EMOJI: Final[str] = ":mag:"
_TOP_APPS: Final[int] = 3
_TOP_KEYWORDS: Final[int] = 5
_MIN_KEYWORD_LENGTH: Final[int] = 4


def _tokenise(text: str) -> list[str]:
    """Whitespace split + non-alphanumeric strip + lowercase.

    Local copy of the same tokenizer used by :mod:`app.keywords` —
    inlined rather than imported because the keywords module keeps it
    private (and ruff's "private import" lint would fire on a direct
    reuse).  Behaviour is identical: ``str.isalnum`` is Unicode-aware
    so Cyrillic and Latin share the same code path.
    """
    tokens: list[str] = []
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append(cleaned.lower())
    return tokens


def _parse_day(day_iso: str) -> date:
    """Validate the ``YYYY-MM-DD`` input and return a :class:`date`.

    Raises :class:`ValueError` with a human-readable message on bad
    input so route / CLI layers can translate it into a 400 or a
    non-zero exit cleanly — same convention as
    :mod:`app.ocr_txt_export`.
    """
    try:
        return datetime.strptime(day_iso, "%Y-%m-%d").date()
    except ValueError as exc:
        msg = f"invalid day {day_iso!r} (expected YYYY-MM-DD)"
        raise ValueError(msg) from exc


def _format_duration(seconds: int) -> str:
    """Render ``seconds`` as a compact ``Hh MMm`` / ``MMm`` string.

    Slack reads best when durations are short — we drop the hour
    segment entirely for sub-hour buckets ("42m") and the minute
    segment for clean-hour buckets ("3h"), matching how humans say
    these out loud.
    """
    safe = max(int(seconds), 0)
    minutes_total = safe // 60
    hours, minutes = divmod(minutes_total, 60)
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes:02d}m"


async def _day_shots_total(day_key: str) -> int:
    """Return the total number of screenshots captured on ``day_key``.

    Counted independently of ``time_on_app`` because that helper drops
    rows with an empty ``app_name`` — we still want those in the header
    so the Slack post matches the dashboard's "N captures today" tile.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots WHERE DATE(captured_at) = ?",
            (day_key,),
        )
        row = await cursor.fetchone()
    return int(row["n"]) if row else 0


async def _day_keywords(day_key: str, top_n: int) -> list[tuple[str, int]]:
    """Top ``top_n`` keywords for the given day from OCR text + notes.

    A local, day-scoped version of :func:`app.keywords.top_keywords`
    (which operates on a rolling N-day window keyed by ``captured_at >=``).
    Reuses the same tokenizer + stopword set so a Slack post looks
    identical in tone to the /stats/keywords page.
    """
    counter: Counter[str] = Counter()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (day_key,),
        )
        async for row in cursor:
            for token in _tokenise(str(row["ocr_text"])):
                if len(token) < _MIN_KEYWORD_LENGTH or token in STOPWORDS:
                    continue
                counter[token] += 1

        # ``screenshot_notes`` has no ``captured_at`` of its own — we
        # join to the parent ``screenshots`` row so the day filter uses
        # the screenshot's capture time, not the note's create time.
        cursor = await conn.execute(
            "SELECT n.body FROM screenshot_notes n "
            "JOIN screenshots s ON s.id = n.screenshot_id "
            "WHERE DATE(s.captured_at) = ?",
            (day_key,),
        )
        async for row in cursor:
            for token in _tokenise(str(row["body"])):
                if len(token) < _MIN_KEYWORD_LENGTH or token in STOPWORDS:
                    continue
                counter[token] += 1

    return list(counter.most_common(top_n))


def _render(
    day_key: str,
    total_shots: int,
    total_seconds: int,
    top_apps: list[dict[str, object]],
    top_keywords: list[tuple[str, int]],
) -> str:
    """Assemble the multi-line Slack-friendly Markdown string.

    Layout::

        ```
        :calendar: 2026-06-03 · Persona · 142 shots · 73 min
        ```
        :camera_with_flash: *Code* — 1h 12m
        :camera_with_flash: *Chrome* — 35m
        :camera_with_flash: *Slack* — 18m
        :mag: top: kubernetes, deployment, …

    The header sits inside a fenced block so Slack renders it as a
    one-line monospaced strip — the rest is regular Markdown.  No
    trailing newline so the caller controls line endings on disk.
    """
    total_minutes = max(int(total_seconds), 0) // 60
    header = (
        f"{_HEADER_EMOJI} {day_key} · Persona · "
        f"{total_shots} shots · {total_minutes} min"
    )

    lines: list[str] = ["```", header, "```"]

    if top_apps:
        for item in top_apps:
            name = str(item["app_name"]).strip() or "-"
            duration = _format_duration(int(item["seconds"]))  # type: ignore[call-overload]
            lines.append(f"{_APP_EMOJI} *{name}* — {duration}")
    else:
        lines.append(f"{_APP_EMOJI} _no app activity_")

    if top_keywords:
        words = ", ".join(word for word, _ in top_keywords)
        lines.append(f"{_KEYWORDS_EMOJI} top: {words}")
    else:
        lines.append(f"{_KEYWORDS_EMOJI} _no keywords_")

    return "\n".join(lines)


async def slack_style_summary(day_iso: str) -> str:
    """Return a Slack-friendly daily summary for ``day_iso``.

    Pulls three datasets in parallel-ish (sequential async, single
    connection per helper) and composes a compact one-emoji-per-bullet
    string suitable for pasting into a chat channel.  Empty days still
    produce a well-formed message ("0 shots · 0 min" plus placeholder
    bullets) so downstream consumers can blindly post the result.
    """
    target = _parse_day(day_iso)
    day_key = target.isoformat()

    raw_apps = await daily_time_on_app(day_key)
    top_apps = raw_apps[:_TOP_APPS]
    total_seconds = sum(int(item["seconds"]) for item in raw_apps)  # type: ignore[call-overload]

    total_shots = await _day_shots_total(day_key)
    top_keywords = await _day_keywords(day_key, _TOP_KEYWORDS)

    body = _render(day_key, total_shots, total_seconds, top_apps, top_keywords)

    log.info(
        "slack_summary.rendered",
        day=day_key,
        total_shots=total_shots,
        total_seconds=total_seconds,
        apps_returned=len(top_apps),
        keywords_returned=len(top_keywords),
        bytes=len(body.encode("utf-8")),
    )
    return body


__all__ = ["slack_style_summary"]
