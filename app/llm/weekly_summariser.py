"""Weekly summary generator — feeds a Mon-Sun window into BYO LLM.

Pulls screenshots, screenshot notes, and any daily digests already produced
inside the target ISO week, then asks the LLM for a tight retrospective with
"Big themes", "Notable moments", "What I shipped" sections.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from app.llm.client import CompletionRequest, LLMClient, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.time import iso

log = get_logger("persona.llm")

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

    since = datetime.combine(monday, time.min, tzinfo=timezone.utc)
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
    request = CompletionRequest(system=_SYSTEM, user=user_message, max_tokens=900)

    log.info(
        "llm.weekly_summary.start",
        week_start=monday_iso,
        events=len(events),
        notes=len(notes),
        daily_digests=len(daily_digests),
        provider=ll.provider,
    )
    text = await ll.complete(request)
    log.info("llm.weekly_summary.done", week_start=monday_iso, chars=len(text))
    return text
