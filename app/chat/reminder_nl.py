"""Разбор напоминаний из естественного языка (ROADMAP S3b-2).

«напомни завтра позвонить маме», «через 3 дня оплатить хостинг», «в пятницу
созвон» → (текст, дата). Детерминированные правила (быстро, без LLM, покрыты
тестами) — основной путь; GBNF/Ollama — необязательное усиление для размытых
формулировок. Таблица reminders хранит дату (без времени), поэтому время суток
(«в 15:00») остаётся в тексте напоминания как есть.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

# Дни недели (рус, в т.ч. сокращения и предложный падеж) → индекс weekday() 0..6.
_WEEKDAYS: dict[str, int] = {
    "понедельник": 0, "пн": 0,
    "вторник": 1, "вт": 1,
    "среду": 2, "среда": 2, "ср": 2,
    "четверг": 3, "чт": 3,
    "пятницу": 4, "пятница": 4, "пт": 4,
    "субботу": 5, "суббота": 5, "сб": 5,
    "воскресенье": 6, "вс": 6,
}

# Слова-триггеры, которые срезаем из начала тела напоминания.
_TRIGGER_RE = re.compile(
    r"^\s*(?:пожалуйста[,\s]+)?"
    r"(?:поставь(?:\s+напоминание)?|запланируй|создай\s+напоминание|"
    r"напомни(?:\s+мне)?|напоминание|todo|задача|надо|нужно)\b[:,\s]*",
    re.IGNORECASE,
)

_DDMM_RE = re.compile(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b")
_THROUGH_DAYS_RE = re.compile(r"\bчерез\s+(\d+)\s*(дн|день|дня|дней)\b", re.IGNORECASE)
_THROUGH_WEEKS_RE = re.compile(
    r"\bчерез\s+(\d+)?\s*(недел[юияь])\b", re.IGNORECASE
)


def _clean_body(text: str, *, remove: list[str]) -> str:
    """Убрать из тела найденные дата-фразы и слова-триггеры, подчистить пробелы."""
    body = text
    for frag in remove:
        if frag:
            body = body.replace(frag, " ", 1)
    body = _TRIGGER_RE.sub("", body)
    body = re.sub(r"\s{2,}", " ", body).strip(" \t\n,.:;—-")
    return body


def parse_reminder(text: str, *, today: date | None = None) -> dict[str, Any]:
    """NL → {body, due_date(ISO), matched_date(bool)}.

    Всегда возвращает результат: если явной даты нет — due_date = сегодня и
    matched_date=False (вызывающий может уточнить). Детерминированно.
    """
    base = today or date.today()
    raw = (text or "").strip()
    due = base
    matched = False
    remove: list[str] = []

    low = raw.lower()

    # 1) абсолютная дата DD.MM(.YYYY)
    m = _DDMM_RE.search(raw)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        try:
            if year:
                y = int(year)
                if y < 100:
                    y += 2000
                cand = date(y, month, day)
            else:
                cand = date(base.year, month, day)
                if cand < base:  # дата уже прошла в этом году → следующий год
                    cand = date(base.year + 1, month, day)
            due, matched = cand, True
            remove.append(m.group(0))
        except ValueError:
            pass

    # 2) относительные слова
    if not matched:
        if "послезавтра" in low:
            due, matched = base + timedelta(days=2), True
            remove.append("послезавтра")
        elif "завтра" in low:
            due, matched = base + timedelta(days=1), True
            remove.append("завтра")
        elif "сегодня" in low:
            due, matched = base, True
            remove.append("сегодня")

    # 3) «через N дней» / «через N недель» / «через неделю»
    if not matched:
        md = _THROUGH_DAYS_RE.search(raw)
        if md:
            due, matched = base + timedelta(days=int(md.group(1))), True
            remove.append(md.group(0))
        else:
            mw = _THROUGH_WEEKS_RE.search(raw)
            if mw:
                n = int(mw.group(1)) if mw.group(1) else 1
                due, matched = base + timedelta(weeks=n), True
                remove.append(mw.group(0))

    # 4) день недели («в пятницу») — ближайший будущий (если совпал с сегодня → +7)
    if not matched:
        for word, wd in _WEEKDAYS.items():
            if re.search(rf"\b{word}\b", low):
                delta = (wd - base.weekday()) % 7
                delta = delta or 7
                due, matched = base + timedelta(days=delta), True
                # срезаем сам день и предлог «в/во» перед ним
                remove.append(word)
                break

    body = _clean_body(raw, remove=remove)
    # подчистить висящий предлог «в/во» от «в пятницу», «на»
    body = re.sub(r"\b(во?|на)\s*$", "", body, flags=re.IGNORECASE).strip(" ,.:;—-")
    if not body:
        body = raw.strip() or "напоминание"
    return {"body": body, "due_date": due.isoformat(), "matched_date": matched}


__all__ = ["parse_reminder"]
