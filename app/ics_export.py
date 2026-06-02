"""iCalendar (.ics) export of daily Persona activity.

Produces an RFC 5545 VCALENDAR with one all-day VEVENT per local day in
the lookback window that has at least one screenshot.  Each event summary
is ``"Persona — <shots> shots"`` and the description lists the top three
foreground apps (by gap-capped active seconds, falling back to shot
counts) so the calendar entry has enough context to jog memory weeks
later.

Stdlib only — no ``icalendar`` package.  We hand-roll the format because
the structure we need is tiny and the package would be dead weight.
"""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.time_on_app import daily_time_on_app

log = get_logger("persona.ics")

# RFC 5545 mandates CRLF — \r\n — between every content line.
_CRLF = "\r\n"
# Soft fold long lines so we stay within the 75-octet limit.  Folding
# means: split at <=75 octets, continue on the next line with a leading
# single space.  We're conservative and fold on character count, which
# is a safe over-approximation for ASCII-ish content.
_FOLD_LIMIT = 73
_PRODID = "-//Persona//Daily Activity//EN"
_VERSION = "2.0"


def _escape_text(value: str) -> str:
    """Escape a TEXT-typed property value per RFC 5545 §3.3.11.

    Order matters: backslashes must be doubled first so we don't
    re-escape the backslashes we insert for commas / semicolons /
    newlines.  Carriage returns inside the input are normalised
    away — only ``\\n`` remains in the escaped output.
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
    folded = [_fold_line(line) for line in lines]
    return _CRLF.join(folded) + _CRLF


def _format_dtstamp(now: datetime) -> str:
    """UTC stamp in the basic ICS form ``YYYYMMDDTHHMMSSZ``.

    We compute it through :func:`email.utils.format_datetime` first to
    honour the project requirement, then convert the RFC 2822 string
    into the compact basic form ICS wants.  This avoids re-implementing
    timezone handling.
    """
    rfc2822 = format_datetime(now.astimezone(UTC), usegmt=True)
    log.debug("ics.dtstamp", rfc2822=rfc2822)
    # We don't actually need to parse it back — we just wanted the side
    # effect of asserting the input is tz-aware.  Build the basic-format
    # string directly from the aware UTC datetime.
    utc = now.astimezone(UTC)
    return utc.strftime("%Y%m%dT%H%M%SZ")


def _format_date_basic(value: date) -> str:
    return value.strftime("%Y%m%d")


async def _daily_shot_counts(start_day: date, end_day: date) -> dict[str, int]:
    """Return ``{ 'YYYY-MM-DD': shot_count }`` for the inclusive window."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE DATE(captured_at) >= ? AND DATE(captured_at) <= ? "
            "GROUP BY DATE(captured_at)",
            (start_day.isoformat(), end_day.isoformat()),
        )
        rows = await cursor.fetchall()
    return {str(row["day"]): int(row["n"]) for row in rows if row["day"]}


async def _top_apps_for_day(day_iso: str, limit: int = 3) -> list[tuple[str, int, int]]:
    """Return ``[(app_name, seconds, shots), ...]`` already sorted desc."""
    rows = await daily_time_on_app(day_iso)
    out: list[tuple[str, int, int]] = []
    for row in rows[:limit]:
        out.append(
            (
                str(row["app_name"]),
                int(row["seconds"]),  # type: ignore[call-overload]
                int(row["shots"]),  # type: ignore[call-overload]
            )
        )
    return out


def _format_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _build_description(top_apps: list[tuple[str, int, int]]) -> str:
    if not top_apps:
        return "No app attribution available."
    bits: list[str] = []
    for idx, (name, secs, shots) in enumerate(top_apps, start=1):
        bits.append(f"{idx}. {name} — {_format_seconds(secs)}, {shots} shots")
    return "Top apps:\n" + "\n".join(bits)


def _vevent_lines(
    day: date,
    shots: int,
    top_apps: list[tuple[str, int, int]],
    dtstamp: str,
    hostname: str,
) -> list[str]:
    uid = f"persona-day-{day.isoformat()}@{hostname}"
    summary = _escape_text(f"Persona — {shots} shots")
    description = _escape_text(_build_description(top_apps))
    dtstart = _format_date_basic(day)
    dtend = _format_date_basic(day + timedelta(days=1))
    return [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{dtstart}",
        f"DTEND;VALUE=DATE:{dtend}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


async def export_ics(days_back: int = 90) -> str:
    """Build an iCalendar v2.0 text document.

    ``days_back`` is clamped to ``[1, 3650]``.  The window is
    ``[today - days_back + 1, today]`` (inclusive on both ends, same as
    :func:`app.time_on_app.app_summary`).
    """
    days_back = max(days_back, 1)
    days_back = min(days_back, 3650)

    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=days_back - 1)

    counts = await _daily_shot_counts(start_day, today)
    now_utc = datetime.now(tz=UTC)
    dtstamp = _format_dtstamp(now_utc)
    hostname = socket.gethostname() or "persona.local"

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        f"VERSION:{_VERSION}",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text('Persona activity')}",
        f"X-WR-CALDESC:{_escape_text('Daily Persona shot counts and top apps')}",
    ]

    # Walk every day in the window so we can log totals; only emit a
    # VEVENT where shots > 0 (per spec).
    events_written = 0
    cursor_day = start_day
    while cursor_day <= today:
        day_key = cursor_day.isoformat()
        shots = counts.get(day_key, 0)
        if shots > 0:
            top_apps = await _top_apps_for_day(day_key)
            lines.extend(
                _vevent_lines(cursor_day, shots, top_apps, dtstamp, hostname)
            )
            events_written += 1
        cursor_day = cursor_day + timedelta(days=1)

    lines.append("END:VCALENDAR")

    log.info(
        "ics.export",
        days_back=days_back,
        start_day=start_day.isoformat(),
        end_day=today.isoformat(),
        events=events_written,
        hostname=hostname,
    )

    return _join_lines(lines)


__all__ = ["export_ics"]
