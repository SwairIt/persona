"""iCalendar (.ics) export of completed Pomodoro focus sessions.

Walks the ``focus_session`` table (created by migration ``036_focus_sessions``
— see :mod:`app.focus` for the async API on the same table) and emits an
RFC 5545 ``VCALENDAR`` containing one ``VEVENT`` per completed session in
the lookback window. Users import the file into Google Calendar / Apple
Calendar / Outlook to overlay their deep-work blocks against meetings.

Distinct from :mod:`app.ics_export` (daily activity rollups). That module
emits one all-day event per day; this one emits a timed event per focus
session so the calendar app can draw the actual time-on-task block.

Stdlib only. Hand-rolled VCALENDAR text instead of the ``icalendar``
package — the format we need is small, the dependency would be dead
weight, and the daily-rollup exporter already established the pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.focus_ics")

# RFC 5545 §3.1 — every content line is terminated by CRLF, no exceptions.
_CRLF = "\r\n"
# Soft-fold lines longer than 75 octets per RFC 5545 §3.1. We fold on
# character count which is a safe over-approximation for ASCII-ish text;
# the continuation line starts with a single leading space.
_FOLD_LIMIT = 73
_PRODID = "-//Persona//Focus sessions//EN"
_VERSION = "2.0"
# Static host suffix for the UID. ``persona.local`` is the convention the
# daily-rollup exporter uses too; keeping it static (instead of
# ``socket.gethostname()``) means the UID is stable across machines so
# re-importing on a different device doesn't duplicate events.
_UID_HOST = "persona.local"

# Lookback window guard rails. ``1`` matches the route's lower bound and
# ``3650`` (~10 years) mirrors :mod:`app.ics_export` so the two exporters
# cap at the same ceiling.
_MIN_DAYS = 1
_MAX_DAYS = 3650


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
    """Apply RFC 5545 line folding to a single content line."""
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
    """Fold each line then join with CRLFs and a trailing CRLF."""
    folded = [_fold_line(line) for line in lines]
    return _CRLF.join(folded) + _CRLF


def _format_dtstamp(now: datetime) -> str:
    """UTC stamp in the basic ICS form ``YYYYMMDDTHHMMSSZ``."""
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO 8601 timestamp into a tz-aware UTC :class:`datetime`.

    The storage layer writes ``datetime.now(UTC).isoformat()`` (see
    :func:`app.focus.start_session`) so the input is always tz-aware
    UTC. A naive value would indicate corruption — we tag it as UTC
    rather than guess a zone so the calendar event lands at the right
    wall-clock time on import.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("focus_ics.parse.failed", value=value[:40])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_dt_basic(value: datetime) -> str:
    """Format a tz-aware :class:`datetime` as ``YYYYMMDDTHHMMSSZ``."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _build_description(
    session_id: int,
    label: str | None,
    work_minutes: int,
    break_minutes: int,
) -> str:
    """Compose the VEVENT DESCRIPTION body.

    ``focus_session`` doesn't have an ``outcome`` column (the schema only
    stores ``label`` + the work/break split), so we synthesise a short
    multi-line summary from the columns we *do* have. The label becomes
    a leading line if present; the work/break split and the session id
    follow so a calendar entry has enough context weeks later.
    """
    lines: list[str] = []
    if label:
        lines.append(label.strip())
    detail = f"Work {work_minutes} min"
    if break_minutes > 0:
        detail += f" · break {break_minutes} min"
    lines.append(detail)
    lines.append(f"Persona focus session #{session_id}")
    return "\n".join(lines)


def _vevent_lines(
    session_id: int,
    start_at: datetime,
    end_at: datetime,
    label: str | None,
    work_minutes: int,
    break_minutes: int,
    dtstamp: str,
) -> list[str]:
    """Render one ``VEVENT`` block for the given completed session."""
    uid = f"persona-focus-{session_id}@{_UID_HOST}"
    summary_raw = label.strip() if label and label.strip() else "Focus session"
    summary = _escape_text(summary_raw)
    description = _escape_text(
        _build_description(session_id, label, work_minutes, break_minutes),
    )
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_format_dt_basic(start_at)}",
        f"DTEND:{_format_dt_basic(end_at)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ]


async def build_focus_ics(days: int = 90) -> str:
    """Build an iCalendar document of completed focus sessions.

    ``days`` is clamped to ``[1, 3650]``. Only rows where
    ``completed = 1`` are emitted, and rows missing ``ended_at`` are
    skipped (a completed session must have an end timestamp — anything
    else is a partial write and would produce a zero-length event).

    The SQL uses SQLite's ``DATE(?, ?)`` modifier form so the lookback
    cutoff is computed inside the database; the bound parameters are
    ``'now'`` and ``f'-{days} days'``, which keeps the query both
    parametrised and tz-consistent with SQLite's UTC default for
    ``DATE``.
    """
    days = max(days, _MIN_DAYS)
    days = min(days, _MAX_DAYS)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, started_at, ended_at, work_minutes, break_minutes, label "
            "FROM focus_session "
            "WHERE completed = 1 AND started_at >= DATE(?, ?) "
            "ORDER BY started_at DESC",
            ("now", f"-{days} days"),
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
        f"X-WR-CALNAME:{_escape_text('Persona focus sessions')}",
        f"X-WR-CALDESC:{_escape_text('Completed Pomodoro focus blocks')}",
    ]

    events_written = 0
    skipped_no_end = 0
    skipped_bad_time = 0
    for row in rows:
        session_id = int(row["id"])
        started_at_raw = str(row["started_at"])
        ended_at_value = row["ended_at"]
        if ended_at_value is None:
            skipped_no_end += 1
            continue
        start_dt = _parse_iso_utc(started_at_raw)
        end_dt = _parse_iso_utc(str(ended_at_value))
        if start_dt is None or end_dt is None:
            skipped_bad_time += 1
            continue
        # Defensive: if the clock went backwards mid-session
        # ``ended_at`` could land before ``started_at``. Calendar apps
        # reject (or silently drop) such events, so we pin the end one
        # second after the start to keep the import lossless.
        if end_dt <= start_dt:
            end_dt = datetime.fromtimestamp(
                start_dt.timestamp() + 1,
                tz=UTC,
            )
        label_raw = row["label"]
        label = str(label_raw) if label_raw is not None else None
        work_minutes = int(row["work_minutes"])
        break_minutes = int(row["break_minutes"])
        lines.extend(
            _vevent_lines(
                session_id=session_id,
                start_at=start_dt,
                end_at=end_dt,
                label=label,
                work_minutes=work_minutes,
                break_minutes=break_minutes,
                dtstamp=dtstamp,
            ),
        )
        events_written += 1

    lines.append("END:VCALENDAR")

    log.info(
        "focus_ics.export",
        days=days,
        rows=len(rows),
        events=events_written,
        skipped_no_end=skipped_no_end,
        skipped_bad_time=skipped_bad_time,
    )

    return _join_lines(lines)


__all__ = ["build_focus_ics"]
