"""This-day-last-year replay — shots + notes + pin from N years ago today.

A small "memory anniversary" view: for each of the requested ``years_back``
offsets (default 1, 2, 3) we pull the screenshots, standalone notes and
:mod:`app.daily_pin` row whose calendar date equals
``today - years_back years``. The payload is intentionally tiny — just
enough for a single timeline card per year — so the page renders cheaply
even on a heavily-loaded laptop.

Date math notes
---------------
``date.replace(year=…)`` raises :class:`ValueError` when the source date
is February 29 and the target year is not a leap year. We catch that
specific case and fall back to February 28 of the target year so a user
opening the page on Feb 29 still gets a coherent "last year on Feb 28"
card instead of a 500. Every other ``ValueError`` is re-raised — we want
genuine bugs (e.g. an out-of-range year) to stay loud.

SQLite is queried with parametrised statements only; the ``years_back``
list is validated to be positive integers before any query runs, so an
attacker who somehow influences the input still cannot reach the SQL
layer with anything but a bounded integer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.this_day_replay")

# Hard cap on ``limit_per_year`` so a hand-crafted URL on the JSON
# endpoint can't ask for a million-row payload. The HTML page never
# renders more than ~30 thumbnails per year anyway.
_MAX_LIMIT_PER_YEAR = 200

# Hard cap on the ``years_back`` list size — the UI only shows 1/2/3
# but the function accepts an arbitrary list so a future widget can ask
# for more. Capping here keeps the worst-case query count bounded.
_MAX_YEARS_BACK_ENTRIES = 25


class SampleShot(TypedDict):
    id: int
    captured_at: str
    app_name: str | None
    thumbnail_path: str | None
    ocr_preview: str


class SampleNote(TypedDict):
    id: int
    title: str | None
    body: str
    created_at: str


class YearReplay(TypedDict):
    years_back: int
    date: str
    total_shots: int
    sample_shots: list[SampleShot]
    daily_pin_text: str | None
    sample_notes: list[SampleNote]


class ReplayPayload(TypedDict):
    today_iso: str
    replays: list[YearReplay]


def _shift_years(today: date, years_back: int) -> date:
    """Return ``today`` shifted ``years_back`` years into the past.

    Falls back to Feb 28 when ``today`` is Feb 29 and the target year is
    not a leap year. Any other :class:`ValueError` is re-raised — we
    don't want to silently swallow e.g. ``years_back = 100000`` which
    would push the year out of the supported range.
    """
    target_year = today.year - years_back
    try:
        return today.replace(year=target_year)
    except ValueError as exc:
        if today.month == 2 and today.day == 29:
            log.debug(
                "this_day_replay.feb29_fallback",
                today=today.isoformat(),
                target_year=target_year,
            )
            return date(target_year, 2, 28)
        raise ValueError(f"cannot shift {today.isoformat()} back {years_back} years") from exc


def _normalise_years_back(years_back: list[int]) -> list[int]:
    """Validate + dedupe + sort the requested year offsets.

    Negative or zero offsets are dropped (they're not "back" — they're
    today or the future). Anything above ``_MAX_YEARS_BACK_ENTRIES`` is
    truncated so a malicious caller can't fan-out the query count.
    """
    cleaned: list[int] = []
    seen: set[int] = set()
    for raw in years_back:
        value = int(raw)
        if value <= 0:
            continue
        if value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
        if len(cleaned) >= _MAX_YEARS_BACK_ENTRIES:
            break
    cleaned.sort()
    return cleaned


def _clamp_limit(limit_per_year: int) -> int:
    """Clamp ``limit_per_year`` into ``[1, _MAX_LIMIT_PER_YEAR]``."""
    if limit_per_year < 1:
        return 1
    if limit_per_year > _MAX_LIMIT_PER_YEAR:
        return _MAX_LIMIT_PER_YEAR
    return limit_per_year


def _day_bounds(target: date) -> tuple[str, str]:
    """Return ``[start, end]`` ISO-8601 UTC strings covering ``target``."""
    start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def _ocr_preview(ocr_text: str | None, max_chars: int = 160) -> str:
    if not ocr_text:
        return ""
    return ocr_text.strip()[:max_chars]


async def _count_shots(
    conn: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshots WHERE captured_at >= ? AND captured_at <= ?",
        (start_iso, end_iso),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"] or 0)


async def _sample_shots(
    conn: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
    limit: int,
) -> list[SampleShot]:
    cursor = await conn.execute(
        "SELECT id, captured_at, app_name, thumbnail_path, ocr_text "
        "FROM screenshots "
        "WHERE captured_at >= ? AND captured_at <= ? "
        "ORDER BY captured_at ASC "
        "LIMIT ?",
        (start_iso, end_iso, int(limit)),
    )
    rows = await cursor.fetchall()
    return [
        SampleShot(
            id=int(r["id"]),
            captured_at=str(r["captured_at"]),
            app_name=(str(r["app_name"]) if r["app_name"] is not None else None),
            thumbnail_path=(str(r["thumbnail_path"]) if r["thumbnail_path"] is not None else None),
            ocr_preview=_ocr_preview(r["ocr_text"]),
        )
        for r in rows
    ]


async def _daily_pin_text(
    conn: aiosqlite.Connection,
    *,
    day: date,
) -> str | None:
    cursor = await conn.execute(
        "SELECT pin FROM daily_pin WHERE day = ?",
        (day.isoformat(),),
    )
    row = await cursor.fetchone()
    if row is None or row["pin"] is None:
        return None
    return str(row["pin"])


async def _sample_notes(
    conn: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
    limit: int,
) -> list[SampleNote]:
    cursor = await conn.execute(
        "SELECT id, title, body, created_at FROM notes "
        "WHERE created_at >= ? AND created_at <= ? "
        "ORDER BY created_at ASC "
        "LIMIT ?",
        (start_iso, end_iso, int(limit)),
    )
    rows = await cursor.fetchall()
    return [
        SampleNote(
            id=int(r["id"]),
            title=(str(r["title"]) if r["title"] is not None else None),
            body=str(r["body"] or ""),
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


async def _build_year_replay(
    conn: aiosqlite.Connection,
    *,
    today: date,
    years: int,
    limit_per_year: int,
) -> YearReplay:
    target = _shift_years(today, years)
    start_iso, end_iso = _day_bounds(target)

    total_shots = await _count_shots(conn, start_iso=start_iso, end_iso=end_iso)
    sample_shots = await _sample_shots(
        conn,
        start_iso=start_iso,
        end_iso=end_iso,
        limit=limit_per_year,
    )
    pin_text = await _daily_pin_text(conn, day=target)
    sample_notes = await _sample_notes(
        conn,
        start_iso=start_iso,
        end_iso=end_iso,
        limit=limit_per_year,
    )

    return YearReplay(
        years_back=years,
        date=target.isoformat(),
        total_shots=total_shots,
        sample_shots=sample_shots,
        daily_pin_text=pin_text,
        sample_notes=sample_notes,
    )


async def get_replay(
    years_back: list[int] | tuple[int, ...] = (1, 2, 3),
    limit_per_year: int = 30,
) -> ReplayPayload:
    """Return shots + notes + pin from each requested anniversary date.

    Parameters
    ----------
    years_back:
        Year offsets to include. ``(1, 2, 3)`` (the default) renders the
        "1 year ago today", "2 years ago today", "3 years ago today"
        cards. Non-positive values are silently dropped; duplicates are
        deduped; the list is capped at ``_MAX_YEARS_BACK_ENTRIES``.
    limit_per_year:
        Maximum number of sample shots and sample notes returned per
        year card. Clamped into ``[1, _MAX_LIMIT_PER_YEAR]``.
    """
    today = datetime.now(tz=UTC).date()
    cleaned_years = _normalise_years_back(list(years_back))
    clamped_limit = _clamp_limit(int(limit_per_year))

    if not cleaned_years:
        log.info(
            "this_day_replay.no_years_requested",
            today=today.isoformat(),
        )
        return ReplayPayload(today_iso=today.isoformat(), replays=[])

    replays: list[YearReplay] = []
    async with get_connection() as conn:
        for years in cleaned_years:
            year_payload = await _build_year_replay(
                conn,
                today=today,
                years=years,
                limit_per_year=clamped_limit,
            )
            replays.append(year_payload)

    log.info(
        "this_day_replay.built",
        today=today.isoformat(),
        years_back=cleaned_years,
        limit_per_year=clamped_limit,
        total_replay_shots=sum(r["total_shots"] for r in replays),
    )
    return ReplayPayload(today_iso=today.isoformat(), replays=replays)


async def get_replay_for_date(
    target: date,
    years_back: list[int] | tuple[int, ...] = (1, 2, 3),
    limit_per_year: int = 30,
) -> ReplayPayload:
    """Same as :func:`get_replay` but anchored to an explicit calendar date.

    Used by the ``GET /memory/replay/{ymd}`` route so an operator can
    bookmark a specific anniversary view (e.g. on their birthday) and
    revisit it independently of *today*. The ``today_iso`` field in the
    returned payload reflects ``target`` for template compatibility.
    """
    cleaned_years = _normalise_years_back(list(years_back))
    clamped_limit = _clamp_limit(int(limit_per_year))

    if not cleaned_years:
        log.info(
            "this_day_replay.no_years_requested",
            anchor=target.isoformat(),
        )
        return ReplayPayload(today_iso=target.isoformat(), replays=[])

    replays: list[YearReplay] = []
    async with get_connection() as conn:
        for years in cleaned_years:
            year_payload = await _build_year_replay(
                conn,
                today=target,
                years=years,
                limit_per_year=clamped_limit,
            )
            replays.append(year_payload)

    log.info(
        "this_day_replay.built_for_date",
        anchor=target.isoformat(),
        years_back=cleaned_years,
        limit_per_year=clamped_limit,
        total_replay_shots=sum(r["total_shots"] for r in replays),
    )
    return ReplayPayload(today_iso=target.isoformat(), replays=replays)


__all__ = [
    "ReplayPayload",
    "SampleNote",
    "SampleShot",
    "YearReplay",
    "get_replay",
    "get_replay_for_date",
]
