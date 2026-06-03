"""Once-a-week worker that emails the last-7-days stats CSV (v0.57).

Polls every 30 minutes. When local time is Monday at
``settings.weekly_stats_email_hour_local`` and we have not yet sent the
weekly stats email for *today* (local), build a 7-day CSV via
:func:`app.stats_csv.export_stats_csv` and ship it as an
``stats.csv`` attachment through :func:`app.smtp_delivery.send_digest_email`
(the v0.31 BYO SMTP relay, extended in v0.57 to accept attachments).

Idempotency is enforced through a ``weekly_stats_email_last_sent`` row in
``kv_settings``; once the date for *today* (local) has been stored, the
worker refuses to send again until the next Monday. This protects against
the 30-minute poll firing twice inside the same hour, and survives
process restarts because the marker lives in SQLite.

Silent-skip rules — the loop NEVER raises through to the caller, and
operationally degrades to a no-op when:

* ``weekly_stats_email_enabled`` is False (logs once, parks on
  ``stop_event``).
* SMTP is not configured (``send_digest_email`` returns
  ``misconfigured`` / ``disabled`` / ``missing_dep``) — we log a warning
  and skip writing the marker so the next tick can retry once config
  arrives.

The body is a tiny plaintext blurb pointing at the attached CSV; the
real payload is the attachment itself.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.logging_setup import get_logger
from app.settings import get_settings
from app.smtp_delivery import send_digest_email
from app.stats_csv import export_stats_csv
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.weekly_stats_email")

POLL_INTERVAL_SECONDS = 1800.0  # 30 minutes
_LAST_SENT_KEY = "weekly_stats_email_last_sent"
_CSV_DAYS_BACK = 7
_CSV_FILENAME = "stats.csv"
_CSV_MIME = "text/csv"
_MONDAY = 0

# Statuses that mean "the SMTP layer accepted the request" — i.e. we
# should advance the idempotency marker. Everything else (``error``,
# ``misconfigured``, ``missing_dep``, ``disabled``) leaves the marker
# untouched so the next tick can retry once the user finishes setup.
_SUCCESS_STATUSES: frozenset[str] = frozenset({"sent"})


async def run_weekly_stats_email_scheduler(
    controller: CaptureController | None = None,
) -> None:
    """Long-running loop. Yields on ``controller.stop_event``."""
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.weekly_stats_email_enabled:
        log.info("weekly_stats_email.disabled")
        await ctrl.stop_event.wait()
        return

    log.info(
        "weekly_stats_email.started",
        hour=settings.weekly_stats_email_hour_local,
    )

    while not ctrl.stop_event.is_set():
        await beat("weekly-stats-email-scheduler")
        try:
            await _maybe_send()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("weekly_stats_email.failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue


async def _maybe_send() -> None:
    """One poll iteration — decide whether to compose + ship the email."""
    settings = get_settings()
    now_local = datetime.now().astimezone()

    # Fire only on Monday at the configured local hour.
    if now_local.weekday() != _MONDAY:
        return
    if now_local.hour != settings.weekly_stats_email_hour_local:
        return

    today_iso = now_local.date().isoformat()

    async with get_connection() as conn:
        last_sent = await get_kv(conn, _LAST_SENT_KEY)

    if last_sent == today_iso:
        return

    csv_body = await export_stats_csv(days_back=_CSV_DAYS_BACK)
    csv_bytes = csv_body.encode("utf-8")

    subject = f"Persona weekly stats — {today_iso}"
    body = _render_body(today_iso=today_iso, csv_bytes_len=len(csv_bytes))

    log.info(
        "weekly_stats_email.compose",
        today=today_iso,
        csv_bytes=len(csv_bytes),
    )

    result = await send_digest_email(
        subject,
        body,
        attachments=[(_CSV_FILENAME, csv_bytes, _CSV_MIME)],
    )
    status = str(result.get("status", "unknown"))

    if status in _SUCCESS_STATUSES:
        async with get_connection() as conn:
            await set_kv(conn, _LAST_SENT_KEY, today_iso)
        log.info(
            "weekly_stats_email.sent",
            today=today_iso,
            to=result.get("to"),
            csv_bytes=len(csv_bytes),
        )
        return

    # Silent on missing SMTP — log at warning level so it shows up in
    # /admin/health but don't propagate (worker stays alive, next tick
    # retries once the user finishes the setup wizard).
    log.warning(
        "weekly_stats_email.skipped",
        today=today_iso,
        status=status,
    )


def _render_body(*, today_iso: str, csv_bytes_len: int) -> str:
    """Compose the markdown-ish plaintext body shipped to SMTP."""
    return "\n".join(
        [
            f"# Persona weekly stats — {today_iso}",
            "",
            (
                f"Attached: {_CSV_FILENAME} — last 7 days of per-day-per-app "
                f"capture stats ({csv_bytes_len} bytes)."
            ),
            "",
            "Columns: date, app_name, shots, total_idle_seconds, "
            "total_active_seconds, ocr_chars_total, has_tldr.",
            "",
            (
                "Sent by Persona. Toggle PERSONA_WEEKLY_STATS_EMAIL_ENABLED "
                "to opt out."
            ),
        ]
    )


__all__ = ["run_weekly_stats_email_scheduler"]
