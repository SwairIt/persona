"""Weekly summary generator — feeds a Mon-Sun window into BYO LLM.

Pulls screenshots, screenshot notes, and any daily digests already produced
inside the target ISO week, then asks the LLM for a tight retrospective with
"Big themes", "Notable moments", "What I shipped" sections.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.llm.client import CompletionRequest, LLMClient, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, list_screenshots
from app.storage.time import iso

log = get_logger("persona.llm")
_prompt_log = get_logger("persona.digest.prompt")

_SYSTEM = (
    "You are a memory assistant for a single user. You receive a structured "
    "log of one calendar week (Mon-Sun) covering what was on their screen, "
    "any free-text notes they wrote, and per-day digests already generated. "
    "Produce a tight first-person weekly retrospective (250-400 words) with "
    "exactly these three sections, in this order, each on its own line as a "
    "Markdown heading: '## Big themes', '## Notable moments', '## What I "
    "shipped'. Write it in the user's language (Russian if Cyrillic dominates "
    "the source text, English otherwise). Do NOT invent facts not visible in "
    "the input. If a section has no material, say so honestly in one sentence "
    "rather than padding."
)

# Default template body the editor in :mod:`app.web.routes.digest_prompts`
# pre-fills its textarea with. It uses every supported placeholder so a
# user who just clicks "Reset to default" lands on a template that
# already passes the placeholder check on first save.
_DEFAULT_PROMPT_TEMPLATE = (
    _SYSTEM
    + "\n\n"
    + "Week starts on {week_start}. The following sections cover "
    + "{shots_count} screen captures plus any notes and daily digests "
    + "already produced:\n\n{sections}"
)

# Public set of placeholders the prompt template editor must honour.
# Keeping it as a module-level tuple lets the route module import the
# canonical list without re-deriving it (and lets future placeholders
# be added in exactly one place).
PROMPT_PLACEHOLDERS: tuple[str, ...] = ("week_start", "sections", "shots_count")

# Key under which the user-edited template is persisted in ``kv_settings``.
# An empty value (the migration-seeded default) means "use the hard-coded
# prompt instead". Any non-empty value is treated as a Python
# ``str.format`` template and rendered with ``PROMPT_PLACEHOLDERS``.
_PROMPT_KV_KEY = "weekly_digest_prompt_template"


def default_weekly_prompt_template() -> str:
    """Return the canonical default template body.

    Exposed as a function (rather than re-exporting the constant) so the
    editor UI can render it in a "show default" disclosure without
    coupling to module-private names, and so future tests can assert
    against the same string the editor displays.
    """
    return _DEFAULT_PROMPT_TEMPLATE


async def _load_custom_prompt_template() -> str | None:
    """Read the user-edited template from ``kv_settings``.

    Returns ``None`` when no override is configured (key absent or value
    empty), which is the signal to the caller that the hard-coded
    ``_SYSTEM`` prompt should be used instead.
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
    week_start: str,
    sections: str,
    shots_count: int,
) -> str | None:
    """Format ``template`` with the three supported placeholders.

    Returns ``None`` if the template is malformed (unknown placeholder,
    unbalanced brace, etc.) so the caller can transparently fall back to
    the hard-coded prompt rather than crashing the weekly digest job.
    The route layer validates placeholders before saving, so a
    ``None`` return here is a defence-in-depth path for templates that
    were edited directly in the database.
    """
    try:
        return template.format(
            week_start=week_start,
            sections=sections,
            shots_count=shots_count,
        )
    except (KeyError, IndexError, ValueError) as exc:
        _prompt_log.warning(
            "digest.prompt.render_failed",
            error=str(exc),
            length=len(template),
        )
        return None


def _monday(target: date) -> date:
    return target - timedelta(days=target.weekday())


def build_weekly_summary_prompt(
    *,
    week_start: date,
    events: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    daily_digests: list[dict[str, Any]],
) -> str:
    """Render the week as compact text the LLM can ingest."""
    week_end = week_start + timedelta(days=6)
    parts: list[str] = [
        f"Week: {week_start.isoformat()} → {week_end.isoformat()}",
        f"Captures: {len(events)} · Notes: {len(notes)} · Daily digests: {len(daily_digests)}",
        "",
    ]

    if daily_digests:
        parts.append("DAILY DIGESTS (already condensed by the assistant):")
        for d in daily_digests:
            parts.append(f"\n--- {d['day']} ---")
            parts.append(str(d["body"]).strip())
        parts.append("")

    if notes:
        parts.append("USER NOTES:")
        for n in notes[:80]:
            ts = n.get("updated_at") or ""
            body = (n.get("body") or "").strip().replace("\n", " ")
            parts.append(f"- {ts}: {body[:300]}")
        parts.append("")

    if events:
        parts.append("CAPTURE EVENTS (sampled, newest last):")
        for ev in events[:300]:
            ts = ev["captured_at"]
            app = ev.get("app_name") or "?"
            title = ev.get("window_title") or ""
            ocr = (ev.get("ocr_text") or "")[:200]
            parts.append(f"- {ts} [{app}] {title} :: {ocr}")

    return "\n".join(parts) if parts else "(no data for this week)"


async def summarise_week(
    week_start: date,
    client: LLMClient | None = None,
) -> str:
    """Pull a Mon-Sun window and return a markdown weekly retrospective."""
    monday = _monday(week_start)
    ll = client or make_client()

    since = datetime.combine(monday, time.min, tzinfo=UTC)
    until = since + timedelta(days=7)
    next_week_iso = (monday + timedelta(days=7)).isoformat()
    monday_iso = monday.isoformat()

    async with get_connection() as conn:
        shots = await list_screenshots(conn, since=since, until=until, limit=4000)

        cursor = await conn.execute(
            "SELECT screenshot_id, body, updated_at FROM screenshot_notes "
            "WHERE updated_at >= ? AND updated_at < ? "
            "ORDER BY updated_at ASC",
            (iso(since), iso(until)),
        )
        note_rows = await cursor.fetchall()

        cursor = await conn.execute(
            "SELECT day, body FROM daily_digest "
            "WHERE day >= ? AND day < ? ORDER BY day ASC",
            (monday_iso, next_week_iso),
        )
        digest_rows = await cursor.fetchall()

    if not shots and not note_rows and not digest_rows:
        return "No activity captured for this week."

    events = [
        {
            "captured_at": iso(s.captured_at),
            "app_name": s.app_name,
            "window_title": s.window_title,
            "ocr_text": s.ocr_text,
        }
        for s in shots
    ]
    notes = [
        {
            "screenshot_id": int(row["screenshot_id"]),
            "body": str(row["body"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in note_rows
    ]
    daily_digests = [
        {"day": str(row["day"]), "body": str(row["body"])} for row in digest_rows
    ]

    apps_summary = Counter(s.app_name for s in shots if s.app_name).most_common(8)
    header = (
        f"Week {monday_iso} → {(monday + timedelta(days=6)).isoformat()} · "
        f"{len(shots)} captures · {len(notes)} notes · "
        f"{len(daily_digests)} daily digests · "
        f"top apps: {', '.join(f'{a} ({n})' for a, n in apps_summary) or '—'}"
    )

    body = build_weekly_summary_prompt(
        week_start=monday,
        events=events,
        notes=notes,
        daily_digests=daily_digests,
    )
    user_message = f"{header}\n\n{body}"

    # Resolve the system prompt. A non-empty ``kv_settings`` row wins,
    # but only if every placeholder renders cleanly — otherwise we fall
    # back to the hard-coded ``_SYSTEM`` and log a structured warning so
    # the user can spot the broken template in their logs.
    system_prompt: str = _SYSTEM
    custom_template = await _load_custom_prompt_template()
    if custom_template is not None:
        rendered = _render_custom_prompt(
            custom_template,
            week_start=monday_iso,
            sections=body,
            shots_count=len(events),
        )
        if rendered is not None:
            system_prompt = rendered
            _prompt_log.info(
                "digest.prompt.override_active",
                week_start=monday_iso,
                length=len(rendered),
            )
        else:
            _prompt_log.warning(
                "digest.prompt.override_skipped",
                week_start=monday_iso,
                reason="render_failed",
            )

    request = CompletionRequest(system=system_prompt, user=user_message, max_tokens=900)

    log.info(
        "llm.weekly_summary.start",
        week_start=monday_iso,
        events=len(events),
        notes=len(notes),
        daily_digests=len(daily_digests),
        provider=ll.provider,
        prompt_custom=custom_template is not None and system_prompt is not _SYSTEM,
    )
    text = await ll.complete(request)
    log.info("llm.weekly_summary.done", week_start=monday_iso, chars=len(text))
    return text
