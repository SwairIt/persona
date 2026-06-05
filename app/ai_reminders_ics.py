"""iCalendar (.ics) feed for AI-suggested reminders (v1.53).

Surfaces the rows that :mod:`app.workers.ai_reminders_worker` writes
into ``ai_reminder`` (see migration ``131_ai_reminder``) as a
subscribable ``text/calendar`` document. Operators can paste the feed
URL into Apple Calendar / Google Calendar / Outlook so the LLM's
"don't forget X tomorrow" suggestions show up next to their regular
meetings — the calendar client polls the URL on a schedule of its
own, so dismissals and new rows propagate naturally.

Why a separate module from :mod:`app.focus_ics`
-----------------------------------------------
The two exporters target different tables (``focus_session`` vs.
``ai_reminder``) and different consumption models (one-shot download
vs. long-lived subscribe). Keeping the builders apart means each one
can evolve its column projection, lookback policy and PRODID line
without dragging the other along. The line-folding / CRLF helpers
are intentionally re-implemented here rather than imported so a
future change to focus-session formatting can't accidentally break
the reminder feed.

The function is stdlib-only — RFC 5545 is small enough that hand-
rolled output stays simpler than pulling in the ``icalendar``
package.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.ai_reminders_ics")

# RFC 5545 §3.1 — every content line MUST be terminated by CRLF.
_CRLF = "\r\n"
# Soft-fold lines longer than 75 octets per RFC 5545 §3.1. Folding on
# character count is a safe over-approximation for ASCII-ish text;
# continuation lines start with a single leading space.
_FOLD_LIMIT = 75
_PRODID = "-//Persona//AI reminders//EN"
_VERSION = "2.0"
# Static UID host suffix. Mirrors :mod:`app.focus_ics` — keeping it
# constant (instead of ``socket.gethostname()``) means the same row
# resolves to the same UID across machines so re-subscribing on a
# different device doesn't duplicate events.
_UID_HOST = "persona.local"

# Default VEVENT duration when the LLM didn't pin an end time. Half an
# hour is long enough for the event to be visible on the calendar's day
# view at a glance without bleeding into adjacent slots.
_DEFAULT_DURATION_MIN = 30
# Hard cap on the number of events we emit per build. The route is
# polled by external calendar clients so an unbounded SELECT would let a
# noisy worker week balloon the response forever; 200 future reminders
# is well past the useful horizon for a daily-suggestion feed.
_QUERY_LIMIT = 200

# VALARM lead time per severity tier. Matches the worker's vocabulary
# (info / warn / action — see migration ``131_ai_reminder``); ``info``
# gets no reminder so casual suggestions don't ping the phone.
_VALARM_LEAD_MIN: dict[str, int] = {
    "warn": 15,
    "action": 30,
}


def _escape_text(value: str) -> str:
    """Escape a TEXT-typed property value per RFC 5545 §3.3.11.

    Order matters: backslashes must be doubled first so we don't
    re-escape the backslashes we insert for commas / semicolons /
    newlines. Carriage returns inside the input are normalised away —
    only ``\\n`` remains in the escaped output.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    """Apply RFC 5545 line folding to a single content line.

    The 75-char cap counts the first chunk in full; continuation lines
    use one byte for the leading space and so carry one fewer payload
    char to stay under the limit too.
    """
    if len(line) <= _FOLD_LIMIT:
        return line
    chunks: list[str] = []
    start = 0
    first = True
    while start < len(line):
        end = start + (_FOLD_LIMIT if first else _FOLD_LIMIT - 1)
        chunk = line[start:end]
        if first:
            chunks.append(chunk)
            first = False
        else:
            chunks.append(" " + chunk)
        start = end
    return _CRLF.join(chunks)


def _join_lines(lines: list[str]) -> str:
    """Fold each line, then join with CRLFs plus a trailing CRLF.

    The trailing CRLF after ``END:VCALENDAR`` is required by §3.4 —
    omitting it makes some Apple Calendar versions reject the feed
    silently.
    """
    folded = [_fold_line(line) for line in lines]
    return _CRLF.join(folded) + _CRLF


def _format_dt_basic(value: datetime) -> str:
    """Format a tz-aware :class:`datetime` as ``YYYYMMDDTHHMMSSZ``."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _format_dtstamp(now: datetime) -> str:
    """UTC stamp in the basic ICS form ``YYYYMMDDTHHMMSSZ``."""
    return _format_dt_basic(now)


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp into a tz-aware UTC :class:`datetime`.

    The worker writes ``iso(datetime.now(UTC))`` (see
    :mod:`app.web.routes.ai_reminders`), so the input is always
    tz-aware UTC. A naive value would indicate a manual SQL edit — we
    tag it as UTC rather than guess a zone so the calendar event lands
    at the right wall-clock time on import.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("ai_reminders_ics.parse.failed", value=value[:40])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _vevent_lines(
    reminder_id: int,
    title: str,
    body: str | None,
    severity: str,
    start_at: datetime,
    end_at: datetime,
    dtstamp: str,
) -> list[str]:
    """Render one ``VEVENT`` block for the given reminder.

    The ``severity`` value drives an optional ``VALARM`` so the calendar
    client wakes the user the right amount of time before an "action"
    item — "info" items are silent and just appear on the day view.
    """
    uid = f"persona-reminder-{reminder_id}@{_UID_HOST}"
    summary_raw = title.strip() if title.strip() else "AI reminder"
    summary = _escape_text(summary_raw)
    description_raw = body if body and body.strip() else summary_raw
    description = _escape_text(description_raw)
    # CATEGORIES makes filtering inside the calendar client trivial:
    # power users can colour-code Persona reminders without touching the
    # rest of their schedule.
    category = _escape_text(f"Persona AI reminder/{severity}")

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_format_dt_basic(start_at)}",
        f"DTEND:{_format_dt_basic(end_at)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        f"CATEGORIES:{category}",
        "TRANSP:TRANSPARENT",
    ]
    lead = _VALARM_LEAD_MIN.get(severity)
    if lead is not None:
        # VALARM ACTION:DISPLAY is the broadest-compatible alarm form;
        # AUDIO/EMAIL aren't honoured the same way across clients.
        lines.extend(
            [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{summary}",
                f"TRIGGER:-PT{lead}M",
                "END:VALARM",
            ],
        )
    lines.append("END:VEVENT")
    return lines


async def build_reminders_ics(host: str) -> str:
    """Build an iCalendar document of pending AI reminders.

    Selects up to :data:`_QUERY_LIMIT` rows that have a ``due_at`` set
    and have not been dismissed, sorted by the time they fire so the
    calendar client can stream them in chronological order. ``host`` is
    included in the calendar metadata (X-WR-CALDESC) so a subscriber
    glancing at the calendar source can tell which Persona instance
    minted the feed — useful when the same person runs Persona on a
    laptop and a desktop and accidentally subscribes to both.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, title, body, due_at, severity FROM ai_reminder "
            "WHERE due_at IS NOT NULL AND dismissed_at IS NULL "
            "ORDER BY due_at LIMIT ?",
            (_QUERY_LIMIT,),
        )
        rows = list(await cursor.fetchall())

    now_utc = datetime.now(tz=UTC)
    dtstamp = _format_dtstamp(now_utc)

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        f"VERSION:{_VERSION}",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text('Persona AI reminders')}",
        f"X-WR-CALDESC:{_escape_text(f'Pending AI reminders from {host}')}",
    ]

    events_written = 0
    skipped_bad_time = 0
    for row in rows:
        reminder_id = int(row["id"])
        due_raw = str(row["due_at"])
        start_dt = _parse_iso_utc(due_raw)
        if start_dt is None:
            skipped_bad_time += 1
            continue
        end_dt = start_dt + timedelta(minutes=_DEFAULT_DURATION_MIN)
        title_raw = row["title"]
        title = str(title_raw) if title_raw is not None else ""
        body_raw = row["body"]
        body = str(body_raw) if body_raw is not None else None
        severity_raw = row["severity"]
        severity = str(severity_raw) if severity_raw is not None else "info"
        lines.extend(
            _vevent_lines(
                reminder_id=reminder_id,
                title=title,
                body=body,
                severity=severity,
                start_at=start_dt,
                end_at=end_dt,
                dtstamp=dtstamp,
            ),
        )
        events_written += 1

    lines.append("END:VCALENDAR")

    log.info(
        "ai_reminders_ics.export",
        host=host,
        rows=len(rows),
        events=events_written,
        skipped_bad_time=skipped_bad_time,
    )

    return _join_lines(lines)


__all__ = ["build_reminders_ics"]
