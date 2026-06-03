"""Monthly summary generator — feeds a full calendar month into BYO LLM.

Sibling of :mod:`app.llm.weekly_summariser` but at a month granularity. Pulls
every daily digest already produced for the target month, the top-30 OCR/notes
keywords (same tokeniser as :mod:`app.keywords`, v0.28) and the top apps by
capture count, then asks the LLM for a ~500-word first-person narrative.

The month is identified by an ISO ``YYYY-MM`` string. The day window we sweep
is ``[first-of-month, first-of-next-month)`` in UTC, mirroring how the weekly
summariser treats its Mon→Sun window.

v0.75 — the hard-coded system prompt can be overridden via a
``monthly_digest_prompt_template`` row in ``kv_settings``. Mirrors the
weekly editor (v0.56) so power users can re-shape the retrospective without
forking the codebase. An empty value falls back to ``_SYSTEM``; a non-empty
value is treated as a Python ``str.format`` template rendered with
:data:`MONTHLY_PROMPT_PLACEHOLDERS`. Validation lives in the route layer
(:mod:`app.web.routes.digest_prompts`); a render failure here is caught and
logged so a broken template never crashes the monthly digest job.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.keywords import STOPWORDS
from app.llm.client import CompletionRequest, LLMClient, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, list_screenshots
from app.storage.time import iso

log = get_logger("persona.monthly_digest")
_prompt_log = get_logger("persona.digest.monthly_prompt")

_SYSTEM = (
    "You are a memory assistant for a single user. You receive a structured "
    "log of one full calendar month covering the per-day digests already "
    "produced by the assistant, the top recurring keywords from screen OCR "
    "and free-text notes, and the apps that dominated the user's attention. "
    "Produce a first-person monthly retrospective of approximately 500 words "
    "(450-550) with exactly these four sections, in this order, each on its "
    "own line as a Markdown heading: '## The arc of the month', "
    "'## Recurring themes', '## What I shipped', '## What I want to carry "
    "forward'. Write it in the user's language (Russian if Cyrillic dominates "
    "the source text, English otherwise). Treat the keyword and app rankings "
    "as evidence to ground the narrative, not as bullet lists to recite. Do "
    "NOT invent facts not visible in the input. If a section has no material, "
    "say so honestly in one sentence rather than padding."
)

# Default template body the editor in :mod:`app.web.routes.digest_prompts`
# pre-fills its monthly textarea with. It uses every supported placeholder
# so a user who just clicks "Reset to default" lands on a template that
# already passes the placeholder check on first save.
_DEFAULT_PROMPT_TEMPLATE = (
    _SYSTEM
    + "\n\n"
    + "Month is {month}. The following sections cover {shots_count} screen "
    + "captures plus the daily digests, top keywords, and top apps for that "
    + "calendar month:\n\n{sections}"
)

# Public tuple of placeholders the monthly prompt template editor must
# honour. Kept module-level (mirrors ``PROMPT_PLACEHOLDERS`` in the
# weekly summariser) so the route module imports the canonical list
# without re-deriving it.
MONTHLY_PROMPT_PLACEHOLDERS: tuple[str, ...] = ("month", "sections", "shots_count")

# Key under which the user-edited monthly template is persisted in
# ``kv_settings``. An empty value (the migration-seeded default) means
# "use the hard-coded prompt instead". Any non-empty value is treated as
# a Python ``str.format`` template rendered with
# :data:`MONTHLY_PROMPT_PLACEHOLDERS`.
_PROMPT_KV_KEY = "monthly_digest_prompt_template"


def default_monthly_prompt_template() -> str:
    """Return the canonical default monthly template body.

    Exposed as a function (mirrors :func:`default_weekly_prompt_template`)
    so the editor UI can render it in a "show default" disclosure without
    coupling to module-private names, and so future tests can assert
    against the same string the editor displays.
    """
    return _DEFAULT_PROMPT_TEMPLATE


async def _load_custom_prompt_template() -> str | None:
    """Read the user-edited monthly template from ``kv_settings``.

    Returns ``None`` when no override is configured (key absent or value
    empty after stripping), which is the signal to the caller that the
    hard-coded ``_SYSTEM`` prompt should be used instead.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _PROMPT_KV_KEY)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _render_custom_prompt(
    template: str,
    *,
    month: str,
    sections: str,
    shots_count: int,
) -> str | None:
    """Format ``template`` with the three supported monthly placeholders.

    Returns ``None`` if the template is malformed (unknown placeholder,
    unbalanced brace, etc.) so the caller can transparently fall back to
    the hard-coded prompt rather than crashing the monthly digest job.
    The route layer validates placeholders before saving, so a ``None``
    return here is a defence-in-depth path for templates that were
    edited directly in the database.
    """
    try:
        return template.format(
            month=month,
            sections=sections,
            shots_count=shots_count,
        )
    except (KeyError, IndexError, ValueError) as exc:
        _prompt_log.warning(
            "digest.monthly_prompt.render_failed",
            error=str(exc),
            length=len(template),
        )
        return None


def _tokenise(text: str) -> list[str]:
    """Whitespace-split, strip non-alphanumeric, lowercase.

    Local copy of :func:`app.keywords._tokenise` — duplicated rather than
    imported because the original is private to its module. Kept Unicode-aware
    (``str.isalnum`` accepts Cyrillic) so the Russian/English stopword list
    keeps doing its job here.
    """
    tokens: list[str] = []
    for raw in text.split():
        cleaned = "".join(ch for ch in raw if ch.isalnum())
        if cleaned:
            tokens.append(cleaned.lower())
    return tokens


def _parse_month(month_iso: str) -> date:
    """Parse a ``YYYY-MM`` string into the first day of that month.

    Raises :class:`ValueError` for malformed input so callers get the same
    error surface as :func:`datetime.date.fromisoformat`.
    """
    parts = month_iso.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        msg = f"Invalid month format: {month_iso!r}, expected YYYY-MM"
        raise ValueError(msg)
    year = int(parts[0])
    month = int(parts[1])
    if not 1 <= month <= 12:
        msg = f"Invalid month value: {month}"
        raise ValueError(msg)
    return date(year, month, 1)


def _next_month(first_of_month: date) -> date:
    """Return the first day of the calendar month after ``first_of_month``."""
    if first_of_month.month == 12:
        return date(first_of_month.year + 1, 1, 1)
    return date(first_of_month.year, first_of_month.month + 1, 1)


def build_monthly_summary_prompt(
    *,
    month_iso: str,
    daily_digests: list[dict[str, Any]],
    keywords: list[dict[str, Any]],
    top_apps: list[tuple[str, int]],
    shots_count: int,
) -> str:
    """Render the month as compact text the LLM can ingest."""
    parts: list[str] = [
        f"Month: {month_iso}",
        (
            f"Captures: {shots_count} · Daily digests: {len(daily_digests)} · "
            f"Distinct top apps: {len(top_apps)}"
        ),
        "",
    ]

    if top_apps:
        parts.append("TOP APPS (by capture count):")
        for app_name, count in top_apps:
            parts.append(f"- {app_name}: {count}")
        parts.append("")

    if keywords:
        parts.append("TOP KEYWORDS (OCR + notes, after stopword filtering):")
        for entry in keywords:
            parts.append(f"- {entry['word']}: {entry['count']}")
        parts.append("")

    if daily_digests:
        parts.append("DAILY DIGESTS (already condensed by the assistant):")
        for d in daily_digests:
            parts.append(f"\n--- {d['day']} ---")
            parts.append(str(d["body"]).strip())
        parts.append("")

    if len(parts) <= 3:
        return f"Month {month_iso}: (no data captured)"
    return "\n".join(parts)


async def _top_month_keywords(
    *,
    since_iso: str,
    until_iso: str,
    top_n: int = 30,
    min_length: int = 4,
) -> list[dict[str, int | str]]:
    """Compute the top ``top_n`` keywords across OCR + notes for the window.

    Mirrors :func:`app.keywords.top_keywords` but accepts an explicit
    ``[since, until)`` ISO window instead of a "last N days" relative
    look-back, so it can be re-run for any past month idempotently.
    """
    counter: Counter[str] = Counter()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "AND ocr_text IS NOT NULL AND ocr_text != ''",
            (since_iso, until_iso),
        )
        async for row in cursor:
            for token in _tokenise(str(row["ocr_text"])):
                if len(token) < min_length or token in STOPWORDS:
                    continue
                counter[token] += 1

        cursor = await conn.execute(
            "SELECT body FROM screenshot_notes "
            "WHERE created_at >= ? AND created_at < ?",
            (since_iso, until_iso),
        )
        async for row in cursor:
            for token in _tokenise(str(row["body"])):
                if len(token) < min_length or token in STOPWORDS:
                    continue
                counter[token] += 1

    return [
        {"word": word, "count": count}
        for word, count in counter.most_common(top_n)
    ]


async def summarise_month(
    month_iso: str,
    client: LLMClient | None = None,
) -> str:
    """Pull a full calendar month and return a markdown monthly retrospective."""
    first_day = _parse_month(month_iso)
    next_first = _next_month(first_day)
    ll = client or make_client()

    since = datetime.combine(first_day, time.min, tzinfo=UTC)
    until = datetime.combine(next_first, time.min, tzinfo=UTC)
    since_iso = iso(since)
    until_iso = iso(until)
    first_iso = first_day.isoformat()
    next_first_iso = next_first.isoformat()

    async with get_connection() as conn:
        shots = await list_screenshots(conn, since=since, until=until, limit=20000)

        cursor = await conn.execute(
            "SELECT day, body FROM daily_digest "
            "WHERE day >= ? AND day < ? ORDER BY day ASC",
            (first_iso, next_first_iso),
        )
        digest_rows = await cursor.fetchall()

    daily_digests = [
        {"day": str(row["day"]), "body": str(row["body"])} for row in digest_rows
    ]

    if not shots and not daily_digests:
        return f"No activity captured for {month_iso}."

    top_apps = Counter(s.app_name for s in shots if s.app_name).most_common(10)
    keywords = await _top_month_keywords(
        since_iso=since_iso,
        until_iso=until_iso,
        top_n=30,
    )

    last_day = next_first - timedelta(days=1)
    header = (
        f"Month {month_iso} ({first_iso} → {last_day.isoformat()}) · "
        f"{len(shots)} captures · {len(daily_digests)} daily digests · "
        f"top apps: {', '.join(f'{a} ({n})' for a, n in top_apps) or '—'}"
    )

    body = build_monthly_summary_prompt(
        month_iso=month_iso,
        daily_digests=daily_digests,
        keywords=keywords,
        top_apps=top_apps,
        shots_count=len(shots),
    )
    user_message = f"{header}\n\n{body}"

    # Resolve the system prompt. A non-empty ``kv_settings`` row wins,
    # but only if every placeholder renders cleanly; otherwise we fall
    # back to the hard-coded ``_SYSTEM`` and log a structured warning so
    # the user can spot the broken template in their logs.
    system_prompt = _SYSTEM
    prompt_custom = False
    custom_template = await _load_custom_prompt_template()
    if custom_template is not None:
        rendered = _render_custom_prompt(
            custom_template,
            month=month_iso,
            sections=body,
            shots_count=len(shots),
        )
        if rendered is not None:
            system_prompt = rendered
            prompt_custom = True
            _prompt_log.info(
                "digest.monthly_prompt.override_active",
                month=month_iso,
                length=len(rendered),
            )
        else:
            _prompt_log.warning(
                "digest.monthly_prompt.override_skipped",
                month=month_iso,
                reason="render_failed",
            )

    request = CompletionRequest(system=system_prompt, user=user_message, max_tokens=1400)

    log.info(
        "llm.monthly_summary.start",
        month=month_iso,
        shots=len(shots),
        daily_digests=len(daily_digests),
        keywords=len(keywords),
        top_apps=len(top_apps),
        provider=ll.provider,
        prompt_custom=prompt_custom,
    )
    text = await ll.complete(request)
    log.info(
        "llm.monthly_summary.done",
        month=month_iso,
        chars=len(text),
    )
    return text
