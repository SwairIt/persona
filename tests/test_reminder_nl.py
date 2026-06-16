"""NL-разбор напоминаний (ROADMAP S3b-2) — детерминированно с фиксированным today."""

from __future__ import annotations

from datetime import date

from app.chat.reminder_nl import parse_reminder

# Вторник.
T = date(2026, 6, 16)


def test_tomorrow() -> None:
    r = parse_reminder("напомни завтра позвонить маме", today=T)
    assert r["due_date"] == "2026-06-17"
    assert r["matched_date"] is True
    assert r["body"] == "позвонить маме"


def test_today() -> None:
    r = parse_reminder("сегодня купить молоко", today=T)
    assert r["due_date"] == "2026-06-16"
    assert "купить молоко" in r["body"]


def test_after_n_days() -> None:
    r = parse_reminder("через 3 дня оплатить хостинг", today=T)
    assert r["due_date"] == "2026-06-19"
    assert r["body"] == "оплатить хостинг"


def test_after_week() -> None:
    r = parse_reminder("через неделю сдать отчёт", today=T)
    assert r["due_date"] == "2026-06-23"
    assert "отчёт" in r["body"]


def test_absolute_ddmm() -> None:
    r = parse_reminder("напомни 20.06 встреча с Олегом", today=T)
    assert r["due_date"] == "2026-06-20"
    assert "встреча с Олегом" in r["body"]


def test_ddmm_past_rolls_next_year() -> None:
    # 01.01 уже прошло относительно 16 июня → следующий год.
    r = parse_reminder("оплатить 01.01", today=T)
    assert r["due_date"] == "2027-01-01"


def test_weekday_future() -> None:
    r = parse_reminder("в пятницу созвон", today=T)
    due = date.fromisoformat(r["due_date"])
    assert due.weekday() == 4  # пятница
    assert due > T  # строго в будущем
    assert "созвон" in r["body"]


def test_no_date_defaults_today_unmatched() -> None:
    r = parse_reminder("купить хлеб", today=T)
    assert r["matched_date"] is False
    assert r["due_date"] == "2026-06-16"
    assert r["body"] == "купить хлеб"


def test_trigger_words_stripped() -> None:
    r = parse_reminder("поставь напоминание завтра проверить почту", today=T)
    assert r["body"] == "проверить почту"
    assert r["due_date"] == "2026-06-17"
