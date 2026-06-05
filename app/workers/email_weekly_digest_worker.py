"""Weekly email digest scheduler (v1.59).

Sunday-evening companion to :mod:`app.workers.daily_email_scheduler`.
When the operator opts in via ``kv_settings.email_weekly_digest_enabled
= '1'`` this worker mails a recap of the *just-ending* ISO week:

* heading "Persona weekly digest — week of {Monday}",
* LLM-written paragraph from ``weekly_card.llm_summary``,
* 5-7 curated picks from ``weekly_highlight``,
* four-stat delta strip vs the same week one calendar month ago,
* footer link to ``/memory/highlights`` + unsubscribe instructions.

Driven by :class:`app.workers._bases.ClockScheduler` so the existing
"fire once per local-clock hour, idempotent across restarts" guarantee
applies. The marker row in ``kv_settings`` is
``email_weekly_digest_last_fired`` (date string, YYYY-MM-DD).

Toggles
-------
* ``email_weekly_digest_enabled`` (kv) — ``"1"`` = on. Defaults to off
  so the feature is opt-in and a fresh install never silently emails.
* ``email_weekly_digest_hour_local`` (kv) — integer 0..23. Defaults to
  ``19`` (Sun 19:00 local). The Sunday weekday filter is hard-coded
  via :attr:`ClockScheduler.weekday_getter`.

Failure handling
----------------

The job RAISES on any failure (SMTP misconfigured, network blip, body
build error). :class:`ClockScheduler` catches the exception, logs at
``exception`` level, and crucially does NOT advance the per-day
marker — so the next 30-min tick can retry once the underlying
problem (e.g. SMTP config) is fixed. The marker only advances on a
clean ``sent`` outcome, exactly matching the daily-email worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from app.email_weekly_digest import build_weekly_digest_body
from app.logging_setup import get_logger
from app.smtp_delivery import send_digest_email
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.email_weekly_digest")

# kv_settings rows. Editing either name here means editing
# ``app.web.routes.email_weekly_digest_settings`` too.
_KV_HOUR: str = "email_weekly_digest_hour_local"
_KV_ENABLED: str = "email_weekly_digest_enabled"
_MARKER_KV: str = "email_weekly_digest_last_fired"

# Defaults match the spec: Sunday 19:00 local time, disabled.
_DEFAULT_HOUR: int = 19
_SUNDAY: int = 6  # Python's ``date.weekday()`` returns 6 for Sunday.

# 30-minute poll cadence — same as every other ClockScheduler-backed
# worker in the project (digest, daily-email, weekly-stats-email).
_POLL_INTERVAL_SECONDS: int = 1800

# Outcome statuses returned by :func:`send_digest_email` that mean
# "the relay accepted the message". Anything else (misconfigured,
# missing_dep, error, disabled) is treated as a retryable failure.
_SUCCESS_STATUSES: frozenset[str] = frozenset({"sent"})


async def _hour_getter() -> int:
    """Read the configured local-clock hour; fall back to ``19``.

    A malformed value (non-int, out of 0..23) collapses to the default
    so a fat-finger in the settings UI can never park the scheduler
    permanently.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("email_weekly_digest.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("email_weekly_digest.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """Return ``True`` only when the kv row is the literal string ``"1"``.

    Mirrors :mod:`app.workers.ai_reminders_worker` and every other
    opt-in worker in the project — accept ``"1"`` as on, treat
    everything else (empty, ``"0"``, typo) as off.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def _sunday_getter() -> int | None:
    """Constant ``Sunday`` — wrapped as a coroutine for ClockScheduler."""
    return _SUNDAY


def _current_week_start_iso() -> str:
    """Return the Monday of the *current* local ISO week, ISO-formatted.

    When the scheduler fires on a Sunday evening this is the Monday of
    the week that is *just ending* — exactly the week the digest body
    should report on.
    """
    today = datetime.now().astimezone().date()
    from datetime import timedelta  # noqa: PLC0415 — keep import scope local

    return (today - timedelta(days=today.weekday())).isoformat()


async def send_weekly_digest_now() -> dict[str, object]:
    """One-off send, used by the test endpoint + the ClockScheduler job.

    Builds the body for the current local ISO week, ships it via the
    shared SMTP helper, and returns the helper's status dict so the
    caller can render it back to the operator (the API endpoint) or
    log + raise (the scheduler).
    """
    week_start_iso = _current_week_start_iso()
    body_html = await build_weekly_digest_body(week_start_iso)
    subject = f"Persona weekly digest — week of {week_start_iso}"

    # The plaintext fallback is intentionally terse — every modern
    # mail client renders the HTML body, but some clipper rules
    # (e.g. Apple Watch previews) only show the plaintext part.
    body_text = (
        f"Persona weekly digest for the week of {week_start_iso}.\n"
        "Open the message in an HTML-capable client for the full "
        "highlights cards and stats strip.\n"
    )

    log.info(
        "email_weekly_digest.compose",
        week_start=week_start_iso,
        body_bytes=len(body_html.encode("utf-8")),
    )
    result = await send_digest_email(subject, body_text, body_html)
    log.info(
        "email_weekly_digest.send_result",
        week_start=week_start_iso,
        status=result.get("status"),
        to=result.get("to"),
    )
    return result


async def _job_send_weekly_digest() -> None:
    """ClockScheduler job — wraps :func:`send_weekly_digest_now`.

    Raises on every non-``sent`` outcome so the scheduler leaves the
    per-day marker untouched and the next 30-minute tick retries.
    """
    result = await send_weekly_digest_now()
    status = str(result.get("status", "unknown"))
    if status in _SUCCESS_STATUSES:
        return
    log.warning(
        "email_weekly_digest.skipped",
        status=status,
        detail=result.get("error") or result.get("missing") or result.get("hint"),
    )
    raise RuntimeError(f"email_weekly_digest skipped: {status}")


async def run_email_weekly_digest_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="email-weekly-digest",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_send_weekly_digest,
        poll_seconds=_POLL_INTERVAL_SECONDS,
        weekday_getter=_sunday_getter,
    )
    await scheduler.run(stop_event)


__all__ = [
    "run_email_weekly_digest_worker",
    "send_weekly_digest_now",
]
