"""Natural-language date parser for /ask queries.

Detects English and Russian relative-date phrases inside a free-form
prompt and returns a deterministic ISO date range that the upstream
caller (e.g. :mod:`app.llm.qa`) can prepend to the LLM context as a
``Дата: <start>..<end>`` line. The goal is to keep the dependency
list lean — this is a pure-Python regex catalogue, no ``dateparser`` /
``dateutil`` magic.

Public API
----------

* :func:`parse_natural_date` — the only function callers should use.
  Returns a dict shaped::

      {
          "start_iso": "2026-06-04T00:00:00+00:00",
          "end_iso":   "2026-06-04T23:59:59.999999+00:00",
          "matched_phrase": "yesterday",
          "kind": "day",  # or "week" | "month" | "range"
      }

  or ``None`` when no phrase matched.

Supported phrases
-----------------

English:
    ``today``, ``yesterday``, ``day before yesterday``,
    ``this week``, ``last week``, ``this month``, ``last month``,
    ``N days ago``, ``N weeks ago``,
    ``last Monday`` / ``Tuesday`` / ... / ``Sunday``.

Russian:
    ``сегодня``, ``вчера``, ``позавчера``,
    ``на этой неделе`` / ``эта неделя``,
    ``на прошлой неделе`` / ``прошлая неделя``,
    ``в этом месяце`` / ``этот месяц``,
    ``в прошлом месяце`` / ``прошлый месяц``,
    ``N дней назад``, ``N недель назад``.

Examples
--------

>>> from datetime import UTC, datetime
>>> now = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
>>> r = parse_natural_date("what did I do yesterday?", now=now)
>>> r["matched_phrase"], r["kind"]
('yesterday', 'day')
>>> r = parse_natural_date("на прошлой неделе чем занимался?", now=now)
>>> r["kind"]
'week'
>>> r = parse_natural_date("3 days ago I read about Rust", now=now)
>>> r["kind"]
'day'
>>> parse_natural_date("just a normal question", now=now) is None
True

The returned ``start_iso`` / ``end_iso`` use the same timezone as
``now`` and bracket the matched window inclusively
(``00:00:00`` .. ``23:59:59.999999`` for day-shaped windows,
Monday 00:00 .. Sunday 23:59 for week-shaped windows, etc.).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, TypedDict

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger("persona.chrono_parse")


# --- Public types ------------------------------------------------------

Kind = Literal["day", "week", "month", "range"]


class ChronoRange(TypedDict):
    """Result shape of :func:`parse_natural_date`."""

    start_iso: str
    end_iso: str
    matched_phrase: str
    kind: Kind


# --- Helpers -----------------------------------------------------------


def _day_window(day: datetime) -> tuple[datetime, datetime]:
    """Return ``(start_of_day, end_of_day)`` in ``day``'s tz."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=999_999)
    return start, end


def _week_window(any_day_in_week: datetime) -> tuple[datetime, datetime]:
    """Return Monday-00:00 .. Sunday-23:59 around ``any_day_in_week``."""
    monday = any_day_in_week - timedelta(days=any_day_in_week.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6)
    end = sunday.replace(hour=23, minute=59, second=59, microsecond=999_999)
    return start, end


def _month_window(any_day_in_month: datetime) -> tuple[datetime, datetime]:
    """Return first-day-00:00 .. last-day-23:59 around ``any_day_in_month``."""
    first = any_day_in_month.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    # Jump to the next month's first day and step one microsecond back.
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    end = next_first - timedelta(microseconds=1)
    return first, end


def _result(
    start: datetime, end: datetime, phrase: str, kind: Kind
) -> ChronoRange:
    return ChronoRange(
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        matched_phrase=phrase,
        kind=kind,
    )


# --- Weekday tables ----------------------------------------------------

_WEEKDAYS_EN: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Russian weekday names map onto stems so the regex can match accusative
# / prepositional forms ("в понедельник", "во вторник" ...). We only
# need the day index; case agreement is the caller's problem.
_WEEKDAYS_RU: dict[str, int] = {
    "понедельник": 0,
    "вторник": 1,
    "сред": 2,  # среду / среда — both match the "сред" stem.
    "четверг": 3,
    "пятниц": 4,  # пятницу / пятница.
    "суббот": 5,  # субботу / суббота.
    "воскресен": 6,  # воскресенье / воскресение.
}


# --- Regex catalogue ---------------------------------------------------

# Order matters: longer, more specific phrases come first so e.g.
# "day before yesterday" wins over a bare "yesterday".

_PATTERNS_EN: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bday\s+before\s+yesterday\b", re.IGNORECASE), "day_before_yesterday"),
    (re.compile(r"\byesterday\b", re.IGNORECASE), "yesterday"),
    (re.compile(r"\btoday\b", re.IGNORECASE), "today"),
    (re.compile(r"\bthis\s+week\b", re.IGNORECASE), "this_week"),
    (re.compile(r"\blast\s+week\b", re.IGNORECASE), "last_week"),
    (re.compile(r"\bthis\s+month\b", re.IGNORECASE), "this_month"),
    (re.compile(r"\blast\s+month\b", re.IGNORECASE), "last_month"),
    (re.compile(r"\b(\d{1,3})\s+days?\s+ago\b", re.IGNORECASE), "n_days_ago"),
    (re.compile(r"\b(\d{1,3})\s+weeks?\s+ago\b", re.IGNORECASE), "n_weeks_ago"),
    (
        re.compile(
            r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            re.IGNORECASE,
        ),
        "last_weekday",
    ),
]

# Russian patterns — IGNORECASE + the UNICODE flag is on by default in
# Python 3 ``re``, so ``\b`` and ``\w`` already understand Cyrillic.
_PATTERNS_RU: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bпозавчера\b", re.IGNORECASE), "day_before_yesterday"),  # noqa: RUF001
    (re.compile(r"\bвчера\b", re.IGNORECASE), "yesterday"),  # noqa: RUF001
    (re.compile(r"\bсегодня\b", re.IGNORECASE), "today"),  # noqa: RUF001
    (
        re.compile(r"\b(?:на\s+)?эт(?:ой|а)\s+недел[еюая]\b", re.IGNORECASE),  # noqa: RUF001
        "this_week",
    ),
    (
        re.compile(r"\b(?:на\s+)?прошл(?:ой|ая)\s+недел[еюая]\b", re.IGNORECASE),
        "last_week",
    ),
    (
        re.compile(r"\b(?:в\s+)?эт(?:ом|от)\s+месяц[еа]?\b", re.IGNORECASE),  # noqa: RUF001
        "this_month",
    ),
    (
        re.compile(r"\b(?:в\s+)?прошл(?:ом|ый)\s+месяц[еа]?\b", re.IGNORECASE),  # noqa: RUF001
        "last_month",
    ),
    (re.compile(r"\b(\d{1,3})\s+дн(?:я|ей|ь)\s+назад\b", re.IGNORECASE), "n_days_ago"),
    (re.compile(r"\b(\d{1,3})\s+недел[ьи]\s+назад\b", re.IGNORECASE), "n_weeks_ago"),
]


# --- Detection ---------------------------------------------------------


def _detect_lang(text: str) -> Literal["en", "ru"]:
    """Cheap auto-detect: any Cyrillic letter => ``ru``, else ``en``."""
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            return "ru"
    return "en"


def _resolve_last_weekday_en(text: str, now: datetime) -> ChronoRange | None:
    """Match the English ``last <weekday>`` pattern."""
    pat = re.compile(
        r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return None
    target = _WEEKDAYS_EN[m.group(1).lower()]
    # "last Tuesday" => the most recent Tuesday strictly before today.
    delta = (now.weekday() - target) % 7
    if delta == 0:
        delta = 7
    day = now - timedelta(days=delta)
    start, end = _day_window(day)
    return _result(start, end, m.group(0), "day")


def _resolve_last_weekday_ru(text: str, now: datetime) -> ChronoRange | None:
    """Match the Russian ``в прошлый <день_недели>`` pattern."""
    # We accept both "в прошлый понедельник" and the bare "в понедельник"
    # spelling — the latter is colloquial for "last <weekday>" when the
    # weekday is already in the past relative to ``now``.
    stems = "|".join(_WEEKDAYS_RU.keys())
    pat = re.compile(
        rf"\b(?:в|во)\s+(?:прошлый\s+|прошлую\s+)?({stems})\w*\b",
        re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return None
    target = _WEEKDAYS_RU[m.group(1).lower()]
    delta = (now.weekday() - target) % 7
    if delta == 0:
        delta = 7
    day = now - timedelta(days=delta)
    start, end = _day_window(day)
    return _result(start, end, m.group(0), "day")


def _last_month_window(now: datetime) -> tuple[datetime, datetime]:
    # Step into the previous month by going one day before the 1st.
    first_of_this = now.replace(day=1)
    some_day_last_month = first_of_this - timedelta(days=1)
    return _month_window(some_day_last_month)


# Per-kind window resolvers. Each callable takes ``(now, match)`` and
# returns a ``(start, end, output_kind)`` triple. Keeping these in a
# dispatch dict keeps :func:`_resolve_kind` flat instead of a stack of
# ``if`` branches that trip PLR0911.
_KIND_RESOLVERS: dict[
    str, tuple[Callable[[datetime, re.Match[str]], tuple[datetime, datetime]], Kind]
] = {
    "today": (lambda now, _m: _day_window(now), "day"),
    "yesterday": (lambda now, _m: _day_window(now - timedelta(days=1)), "day"),
    "day_before_yesterday": (
        lambda now, _m: _day_window(now - timedelta(days=2)),
        "day",
    ),
    "this_week": (lambda now, _m: _week_window(now), "week"),
    "last_week": (lambda now, _m: _week_window(now - timedelta(days=7)), "week"),
    "this_month": (lambda now, _m: _month_window(now), "month"),
    "last_month": (lambda now, _m: _last_month_window(now), "month"),
    "n_days_ago": (
        lambda now, m: _day_window(now - timedelta(days=int(m.group(1)))),
        "day",
    ),
    "n_weeks_ago": (
        lambda now, m: _week_window(now - timedelta(weeks=int(m.group(1)))),
        "week",
    ),
}


def _resolve_kind(
    kind: str, m: re.Match[str], now: datetime
) -> tuple[datetime, datetime, Kind] | None:
    """Map a tag from the pattern table to a concrete window.

    Returns ``None`` for unknown tags (notably ``last_weekday``), so
    the caller can fall through to the dedicated weekday resolvers —
    the simple per-tag dispatch table cannot express the capture-group
    lookup those need.
    """
    entry = _KIND_RESOLVERS.get(kind)
    if entry is None:
        return None
    resolver, out_kind = entry
    start, end = resolver(now, m)
    return start, end, out_kind


def _apply_patterns(
    patterns: list[tuple[re.Pattern[str], str]],
    text: str,
    now: datetime,
) -> ChronoRange | None:
    """Walk a pattern list in declared order; return the first hit."""
    for pat, kind in patterns:
        m = pat.search(text)
        if not m:
            continue
        resolved = _resolve_kind(kind, m, now)
        if resolved is None:
            continue
        start, end, out_kind = resolved
        return _result(start, end, m.group(0), out_kind)
    return None


# --- Public entry point ------------------------------------------------


def parse_natural_date(
    text: str,
    now: datetime,
    lang: Literal["auto", "en", "ru"] = "auto",
) -> ChronoRange | None:
    """Find the first relative-date phrase in ``text`` and resolve it.

    Args:
        text: Free-form user prompt.
        now: Anchor datetime; resolved ranges are computed against it
            and inherit its timezone.
        lang: ``"en"`` / ``"ru"`` to force a language, or ``"auto"``
            (default) to sniff via Cyrillic-codepoint presence.

    Returns:
        A :class:`ChronoRange` dict, or ``None`` when no known phrase
        was found.
    """
    if not text or not text.strip():
        return None

    effective_lang: Literal["en", "ru"] = (
        _detect_lang(text) if lang == "auto" else lang
    )

    # Try the language-specific catalogue first, then fall through to
    # the other language so mixed-language prompts (e.g. a Russian user
    # typing the word "today") still match.
    primary = _PATTERNS_RU if effective_lang == "ru" else _PATTERNS_EN
    secondary = _PATTERNS_EN if effective_lang == "ru" else _PATTERNS_RU

    hit = _apply_patterns(primary, text, now)
    if hit is None:
        hit = _apply_patterns(secondary, text, now)

    # ``last <weekday>`` cannot be resolved by the simple per-kind table
    # because it depends on the captured group; resolve it explicitly.
    if hit is None:
        hit = _resolve_last_weekday_en(text, now)
    if hit is None:
        hit = _resolve_last_weekday_ru(text, now)

    if hit is not None:
        log.info(
            "chrono_parse.match",
            lang=effective_lang,
            kind=hit["kind"],
            phrase=hit["matched_phrase"],
            start=hit["start_iso"],
            end=hit["end_iso"],
        )
    else:
        log.debug("chrono_parse.no_match", lang=effective_lang, text_len=len(text))

    return hit


__all__ = ["ChronoRange", "Kind", "parse_natural_date"]
