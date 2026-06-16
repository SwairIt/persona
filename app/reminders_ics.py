"""iCalendar (.ics) экспорт пользовательских напоминаний-todo (ROADMAP S4a).

Таблица ``reminders`` (см. app/storage/reminders.py) — это датированные задачи,
которые ставит NL-инструмент ``schedule_reminder`` («напомни завтра …») и ручной
ввод. Здесь отдаём их как ВЕСЬДЕНЬ-события (DTSTART;VALUE=DATE) в стандартном
``text/calendar`` — чтобы подписаться/импортировать в Apple/Google/Outlook
календарь. Local-first: данные не уходят наружу, файл скачивается локально.

Отдельно от :mod:`app.ai_reminders_ics` (та таблица — ``ai_reminder`` c точным
временем due_at; здесь — даты без времени). Хелперы свёртки/экранирования RFC
5545 намеренно переписаны (stdlib-only), чтобы изменения одного экспортёра не
ломали другой.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.reminders_ics")

_CRLF = "\r\n"
_FOLD_LIMIT = 75
_PRODID = "-//Persona//Reminders//RU"
_VERSION = "2.0"
_UID_HOST = "persona.local"
_QUERY_LIMIT = 500


def _escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold_line(line: str) -> str:
    if len(line) <= _FOLD_LIMIT:
        return line
    chunks: list[str] = []
    start = 0
    first = True
    while start < len(line):
        end = start + (_FOLD_LIMIT if first else _FOLD_LIMIT - 1)
        chunk = line[start:end]
        chunks.append(chunk if first else " " + chunk)
        first = False
        start = end
    return _CRLF.join(chunks)


def _join_lines(lines: list[str]) -> str:
    return _CRLF.join(_fold_line(ln) for ln in lines) + _CRLF


def _date_basic(value: date) -> str:
    """ВЕСЬДЕНЬ-дата в форме YYYYMMDD (RFC 5545 §3.3.4)."""
    return value.strftime("%Y%m%d")


def _vevent_lines(rid: int, body: str, due: date, done: bool, dtstamp: str) -> list[str]:
    uid = f"persona-todo-{rid}@{_UID_HOST}"
    summary = _escape_text((body or "напоминание").strip() or "напоминание")
    # ВЕСЬДЕНЬ-событие: DTEND — следующий день (полуоткрытый интервал).
    end = due.toordinal() + 1
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{_date_basic(due)}",
        f"DTEND;VALUE=DATE:{_date_basic(date.fromordinal(end))}",
        f"SUMMARY:{('✓ ' if done else '') + summary}",
        f"CATEGORIES:{_escape_text('Persona напоминание')}",
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]
    return lines


async def build_todo_ics(host: str, *, include_done: bool = False) -> str:
    """Собрать iCalendar из таблицы reminders. include_done=False — только активные."""
    sql = (
        "SELECT id, body, due_date, done FROM reminders "
        + ("" if include_done else "WHERE done = 0 ")
        + "ORDER BY due_date LIMIT ?"
    )
    async with get_connection() as conn:
        cur = await conn.execute(sql, (_QUERY_LIMIT,))
        rows = list(await cur.fetchall())

    dtstamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        f"VERSION:{_VERSION}",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape_text('Persona напоминания')}",
        f"X-WR-CALDESC:{_escape_text(f'Напоминания-задачи из {host}')}",
    ]
    written = 0
    skipped = 0
    for row in rows:
        try:
            due = date.fromisoformat(str(row["due_date"]))
        except (ValueError, TypeError):
            skipped += 1
            continue
        lines.extend(
            _vevent_lines(
                int(row["id"]),
                str(row["body"] or ""),
                due,
                bool(row["done"]),
                dtstamp,
            )
        )
        written += 1
    lines.append("END:VCALENDAR")
    log.info("reminders_ics.export", host=host, rows=len(rows), events=written, skipped=skipped)
    return _join_lines(lines)


__all__ = ["build_todo_ics"]
