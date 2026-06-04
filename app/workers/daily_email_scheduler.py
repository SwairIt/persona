"""Daily summary email worker — emails yesterday's TL;DR plus key stats.

Runs in the same lifespan as every other worker. The loop polls every 30
minutes and, when local-time hour matches ``settings.daily_email_hour_local``,
composes a small digest for the **previous** calendar day and pushes it
through :func:`app.smtp_delivery.send_digest_email` (the v0.31 BYO SMTP
relay).

Idempotency is enforced through a ``daily_email_last_sent`` row in
``kv_settings``; once the date for *today* (local) has been stored, the
worker refuses to send again until tomorrow. This protects against the
30-minute poll firing twice inside the same hour, and survives process
restarts because the marker lives in SQLite.

Silent-skip rules — the loop NEVER raises through to the caller, and
operationally degrades to a no-op when:

* ``daily_email_enabled`` is False (the worker logs once and parks on
  ``stop_event``).
* SMTP is not configured (``send_digest_email`` returns ``misconfigured``
  / ``disabled`` / ``missing_dep`` — we log a warning and skip writing
  the marker so the next tick can retry once config arrives).
* The BYO LLM has no API key — the body falls back to the cached TL;DR
  if one exists, otherwise to the stats-only body. We never block the
  email on a missing LLM.

The body is plain text (markdown-flavoured for readability). The HTML
alternative is intentionally omitted — Persona's SMTP layer treats it as
optional and email clients render the plaintext fine.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from app.llm.day_tldr import summarise_day_tldr
from app.logging_setup import get_logger
from app.settings import get_settings
from app.smtp_delivery import send_digest_email
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.storage.time import iso
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.daily_email")

POLL_INTERVAL_SECONDS = 1800.0  # 30 minutes
_LAST_SENT_KEY = "daily_email_last_sent"

# Statuses that mean "the SMTP layer accepted the request" — i.e. we
# should advance the idempotency marker. Everything else (``error``,
# ``misconfigured``, ``missing_dep``, ``disabled``) leaves the marker
# untouched so the next tick can retry once the user finishes setup.
_SUCCESS_STATUSES: frozenset[str] = frozenset({"sent"})


@dataclass(frozen=True, slots=True)
class _DayStats:
    """Counts the email body interpolates into its text template."""

    shots: int
    top_app: str | None
    top_app_count: int
    current_streak: int
    active_seconds: int
    idle_seconds: int


async def _hour_getter() -> int:
    return int(get_settings().daily_email_hour_local)


async def _enabled_getter() -> bool:
    return bool(get_settings().daily_email_enabled)


async def run_daily_email_scheduler(
    controller: CaptureController | None = None,
) -> None:
    """Lifespan entry point — uses :class:`ClockScheduler` from v1.31."""
    from app.workers._bases import ClockScheduler  # noqa: PLC0415

    ctrl = controller or get_controller()
    scheduler = ClockScheduler(
        name="daily-email-scheduler",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_LAST_SENT_KEY,
        job=_job_send,
        poll_seconds=int(POLL_INTERVAL_SECONDS),
    )
    await scheduler.run(ctrl.stop_event)


async def _job_send() -> None:
    """One job invocation — compose + ship yesterday's digest email.

    The clock-marker (managed by ClockScheduler) keeps us from
    double-firing within the same day. The empty-day branch sets the
    marker explicitly via set_kv so an empty calendar day still counts
    as "handled today" without trying to recompute every tick.
    """
    now_local = datetime.now().astimezone()
    today_iso = now_local.date().isoformat()
    target_day = now_local.date() - timedelta(days=1)
    target_iso = target_day.isoformat()

    async with get_connection() as conn:
        stats = await _gather_stats(conn, target_day)

    if stats.shots == 0:
        log.info("daily_email.empty_day", day=target_iso)
        # Empty day = handled — ClockScheduler advances the marker
        # automatically when we return normally.
        return

    tldr_text = await _safe_tldr(target_iso)

    subject = f"Persona daily — {target_iso}"
    body = _render_body(day_iso=target_iso, tldr=tldr_text, stats=stats)

    log.info(
        "daily_email.compose",
        day=target_iso,
        shots=stats.shots,
        has_tldr=bool(tldr_text),
    )

    result = await send_digest_email(subject, body)
    status = str(result.get("status", "unknown"))

    if status in _SUCCESS_STATUSES:
        log.info("daily_email.sent", day=target_iso, to=result.get("to"))
        return

    # v1.31 — raise so ClockScheduler does NOT advance the day-marker.
    # The next 30-min tick will retry once SMTP is configured.
    log.warning("daily_email.skipped", day=target_iso, status=status)
    raise RuntimeError(f"daily_email skipped: {status}")


async def _safe_tldr(day_iso: str) -> str:
    """Return the day's TL;DR sentence, or an empty string on failure.

    We never block the email on the LLM. The TL;DR is best-effort: when
    BYO LLM is not configured (or the upstream call raises) we ship the
    stats-only body and log a single info line.
    """
    try:
        result = await summarise_day_tldr(day_iso)
    except Exception as exc:
        log.info("daily_email.tldr_failed", day=day_iso, error=str(exc))
        return ""

    if result["status"] == "ok":
        return result["tldr"]
    log.info("daily_email.tldr_skipped", day=day_iso, status=result["status"])
    return ""


async def _gather_stats(
    conn: aiosqlite.Connection,
    target: date,
) -> _DayStats:
    """Roll up the headline numbers shown in the email body."""
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
        "SELECT app_name FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL",
        (since_iso, until_iso),
    )
    app_rows = await cursor.fetchall()
    app_counter: Counter[str] = Counter(
        str(row["app_name"]) for row in app_rows
    )
    top_app, top_app_count = (
        app_counter.most_common(1)[0] if app_counter else (None, 0)
    )

    active_seconds, idle_seconds = await _active_idle_split(
        conn, since_iso=since_iso, until_iso=until_iso
    )
    current_streak = await _current_streak(conn, anchor=target)

    return _DayStats(
        shots=total,
        top_app=top_app,
        top_app_count=top_app_count,
        current_streak=current_streak,
        active_seconds=active_seconds,
        idle_seconds=idle_seconds,
    )


async def _active_idle_split(
    conn: aiosqlite.Connection,
    *,
    since_iso: str,
    until_iso: str,
) -> tuple[int, int]:
    """Estimate active vs idle wall-clock seconds for the day.

    Heuristic: walk consecutive captures, sum gaps under the idle
    threshold as "active" and everything above it as "idle". We use
    ``settings.idle_threshold_seconds`` so the split matches the rest of
    the analytics surface. Returns ``(0, 0)`` when fewer than two shots
    were taken (no gap to measure).
    """
    settings = get_settings()
    idle_threshold = float(settings.idle_threshold_seconds)

    cursor = await conn.execute(
        "SELECT captured_at FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "ORDER BY captured_at ASC",
        (since_iso, until_iso),
    )
    rows = list(await cursor.fetchall())
    if len(rows) < 2:
        return 0, 0

    timestamps: list[datetime] = []
    for row in rows:
        try:
            parsed = datetime.fromisoformat(str(row["captured_at"]))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        timestamps.append(parsed)

    active = 0.0
    idle = 0.0
    for prev, current in pairwise(timestamps):
        gap = (current - prev).total_seconds()
        if gap <= 0:
            continue
        if gap <= idle_threshold:
            active += gap
        else:
            idle += gap

    return int(active), int(idle)


async def _current_streak(
    conn: aiosqlite.Connection,
    *,
    anchor: date,
) -> int:
    """Count consecutive capture-days ending at ``anchor`` (inclusive)."""
    cursor = await conn.execute(
        "SELECT DISTINCT DATE(captured_at) AS day FROM screenshots "
        "ORDER BY day DESC"
    )
    rows = await cursor.fetchall()
    days: set[date] = set()
    for row in rows:
        raw = row["day"]
        if raw is None:
            continue
        try:
            days.add(date.fromisoformat(str(raw)[:10]))
        except ValueError:
            continue

    streak = 0
    cursor_day = anchor
    while cursor_day in days:
        streak += 1
        cursor_day -= timedelta(days=1)
    return streak


def _format_duration(seconds: int) -> str:
    """Pretty-print a duration as ``Hh MMm`` (or ``MMm`` under an hour)."""
    if seconds <= 0:
        return "0m"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h {minutes:02d}m"


def _render_body(*, day_iso: str, tldr: str, stats: _DayStats) -> str:
    """Compose the markdown-ish plaintext body shipped to SMTP."""
    lines: list[str] = [f"# Persona daily summary — {day_iso}", ""]

    if tldr:
        lines.extend(["## TL;DR", tldr, ""])

    lines.append("## Stats")
    lines.append(f"- Captures: {stats.shots}")
    if stats.top_app is not None:
        lines.append(
            f"- Top app: {stats.top_app} ({stats.top_app_count})"
        )
    else:
        lines.append("- Top app: —")
    lines.append(f"- Current streak: {stats.current_streak} day(s)")
    lines.append(
        f"- Active / idle: {_format_duration(stats.active_seconds)} "
        f"/ {_format_duration(stats.idle_seconds)}"
    )
    lines.append("")
    lines.append(
        "Sent by Persona. Toggle PERSONA_DAILY_EMAIL_ENABLED to opt out."
    )
    return "\n".join(lines)


__all__ = ["run_daily_email_scheduler"]
