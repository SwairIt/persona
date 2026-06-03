"""Daily summary generator — feeds last 24h captures into BYO LLM."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.llm.client import CompletionRequest, LLMClient, make_client
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, list_screenshots
from app.storage.time import iso

log = get_logger("persona.llm")
_fomo_log = get_logger("persona.digest.fomo")

# v0.59 — kv key the settings UI checkbox writes to. The digest path
# treats a kv override as the source of truth (since the checkbox in
# /settings is the user-facing control), but falls back to the env flag
# ``PERSONA_ANTI_FOMO_DIGEST`` so headless deployments keep working.
_ANTI_FOMO_KV_KEY = "anti_fomo_digest"


def _parse_bool_flag(raw: str | None) -> bool | None:
    """Return ``True``/``False`` for a kv string, ``None`` if absent."""
    if raw is None:
        return None
    normalised = raw.strip().lower()
    if normalised == "":
        return None
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    return None


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

# v0.59 — anti-FOMO variant. Stripped of every quantitative angle so the
# resulting digest reads like a journal of themes rather than a
# productivity scorecard. Length stays similar so layout does not shift
# when the user toggles the flag on and off.
_SYSTEM_ANTI_FOMO = (
    "You are a memory assistant for a single user. You receive a structured log "
    "of what they had on their screen during one day: timestamps, app names, "
    "window titles, and OCR snippets. Produce a SHORT first-person "
    "QUALITATIVE summary (120-200 words) that describes the THEMES and "
    "TOPICS that ran through the day — what the user thought about, who or "
    "what they engaged with, the texture of the work. Write it in the user's "
    "language (Russian if Cyrillic dominates the OCR text, English otherwise). "
    "Hard rules: do NOT mention any shot count, capture count, percentage, "
    "ratio, time-spent figure, hour total, minute total, duration, "
    "productivity score, app ranking, or any other numeric or "
    "productivity-style metric. Do not say things like 'you spent X on Y' "
    "or 'most of the day'. Focus on themes only. Do NOT invent facts not "
    "supported by the log. If nothing meaningful was captured, say so "
    "honestly in one sentence."
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
        kv_flag = _parse_bool_flag(await get_kv(conn, _ANTI_FOMO_KV_KEY))

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

    # v0.59 — when anti-FOMO mode is on we strip *every* quantitative
    # signal from BOTH the system prompt and the per-day header before
    # the LLM ever sees it. Feeding "12 captures, top apps: …" alongside
    # an instruction to avoid metrics is a classic prompt-injection foot-
    # gun, so the header collapses to a date-only line and the top-apps
    # roll-up is suppressed entirely.
    cfg = get_settings()
    # kv override wins (UI checkbox is the user-facing control), env flag
    # provides the headless default for deployments without a writable UI.
    anti_fomo = kv_flag if kv_flag is not None else bool(cfg.anti_fomo_digest)
    if anti_fomo:
        header = f"Day: {since.date().isoformat()}"
        system_prompt = _SYSTEM_ANTI_FOMO
        _fomo_log.info(
            "digest.fomo.daily.active",
            day=since.date().isoformat(),
            events=len(events),
        )
    else:
        apps_summary = Counter(s.app_name for s in shots if s.app_name).most_common(5)
        header = (
            f"Day: {since.date().isoformat()} · {len(shots)} unique captures · "
            f"top apps: {', '.join(f'{a} ({n})' for a, n in apps_summary)}"
        )
        system_prompt = _SYSTEM

    user_message = f"{header}\n\nEVENTS:\n{build_daily_summary_prompt(events)}"
    request = CompletionRequest(system=system_prompt, user=user_message, max_tokens=600)

    log.info(
        "llm.summary.start",
        events=len(events),
        provider=ll.provider,
        anti_fomo=anti_fomo,
    )
    text = await ll.complete(request)
    log.info("llm.summary.done", chars=len(text), anti_fomo=anti_fomo)
    return text
