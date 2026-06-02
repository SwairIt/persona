"""Daily summary generator — feeds last 24h captures into BYO LLM."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.llm.client import CompletionRequest, LLMClient, make_client
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.time import iso

log = get_logger("persona.llm")

_SYSTEM = (
    "You are a memory assistant for a single user. You receive a structured log "
    "of what they had on their screen during one day: timestamps, app names, "
    "window titles, and OCR snippets. Produce a SHORT first-person summary "
    "(120-200 words) covering: (1) what the user worked on, (2) any decisions "
    "or commitments visible, (3) noteworthy people or projects mentioned, "
    "(4) a one-line guess at the user's mood/focus. Write it in the user's "
    "language (Russian if Cyrillic dominates the OCR text, English otherwise). "
    "Do NOT make up facts not supported by the log. If nothing meaningful was "
    "captured, say so honestly in one sentence."
)


def build_daily_summary_prompt(events: list[dict[str, Any]]) -> str:
    """Render the events as compact text the LLM can ingest."""
    lines: list[str] = []
    for ev in events[:200]:
        ts = ev["captured_at"]
        app = ev.get("app_name") or "?"
        title = ev.get("window_title") or ""
        ocr = (ev.get("ocr_text") or "")[:240]
        lines.append(f"- {ts} [{app}] {title} :: {ocr}")
    return "\n".join(lines) or "(no captures)"


async def summarise_day(target_day: datetime, client: LLMClient | None = None) -> str:
    """Pull all captures for the given day and return a markdown summary."""
    ll = client or make_client()
    since = target_day.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    until = since + timedelta(days=1)

    async with get_connection() as conn:
        shots = await list_screenshots(conn, since=since, until=until, limit=2000)

    if not shots:
        return "No captures for this day."

    events = [
        {
            "captured_at": iso(s.captured_at),
            "app_name": s.app_name,
            "window_title": s.window_title,
            "ocr_text": s.ocr_text,
        }
        for s in shots
    ]

    apps_summary = Counter(s.app_name for s in shots if s.app_name).most_common(5)
    header = (
        f"Day: {since.date().isoformat()} · {len(shots)} unique captures · "
        f"top apps: {', '.join(f'{a} ({n})' for a, n in apps_summary)}"
    )

    user_message = f"{header}\n\nEVENTS:\n{build_daily_summary_prompt(events)}"
    request = CompletionRequest(system=_SYSTEM, user=user_message, max_tokens=600)

    log.info("llm.summary.start", events=len(events), provider=ll.provider)
    text = await ll.complete(request)
    log.info("llm.summary.done", chars=len(text))
    return text
