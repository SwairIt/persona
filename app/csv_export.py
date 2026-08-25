"""Bulk CSV export — paginated streamers for the four power-user tables.

Sibling of the focussed exports already living in
:mod:`app.web.routes.annotations_csv`, :mod:`app.web.routes.app_shots_csv`,
:mod:`app.web.routes.sticky_export` and :mod:`app.web.routes.share_visits_csv`.
Each existing module exports a single, narrow table; this one covers the
four "fat" tables a typical Persona install will grow into the million-
row range — ``screenshots``, ``screenshot_notes``, ``hourly_card`` and
``audio_segment`` — with a single shared streaming pattern.

Why streaming + id-seek pagination
----------------------------------

A ``fetchall()`` on a million-row table blows past process memory in
seconds and pins the SQLite connection for the duration of the dump,
starving the capture worker. Async generator + ``WHERE id > :last_id
LIMIT :page`` keeps memory bounded (one 1 000-row page at a time) and
lets the cursor be reopened on each page so the connection is held only
briefly. The id-seek is strictly faster than ``OFFSET`` because SQLite
walks the primary-key index forward instead of skipping rows it just
read.

The CSV escape implementation is hand-rolled (rather than the stdlib
:mod:`csv`) so the generators can ``yield`` line-by-line without the
``io.StringIO`` flush + truncate dance — and so the escape contract
stays visible at the call site. RFC 4180 is the target: comma /
quote / newline in a field force quoting, internal quotes double up.

Public API
----------

* :func:`stream_screenshots_csv` — columns ``id, captured_at, app_name,
  window_title, ocr_text, pinned_at, has_alt``. ``has_alt`` is derived
  from ``alt_text IS NOT NULL`` (see migration ``108_shot_alt_text``).
* :func:`stream_notes_csv` — ``screenshot_notes`` (migration ``002``)
  columns ``id, screenshot_id, body, created_at, updated_at``. The
  underlying table uses ``screenshot_id`` as PK; the export reuses it as
  the row id for the canonical seek column.
* :func:`stream_hourly_cards_csv` — ``hourly_card`` (migration ``097``)
  columns ``hour_start, hour_end, summary, screen_count, audio_seconds,
  top_words, llm_enriched, created_at``. The natural key
  ``hour_start`` doubles as the seek cursor — it's a strictly
  increasing ISO-8601 string so lexicographic ordering matches
  chronological ordering.
* :func:`stream_audio_segments_csv` — ``audio_segment`` (migration
  ``092``) columns ``id, started_at, ended_at, duration_s, codec,
  bitrate, transcript, locale``.

Date filters
~~~~~~~~~~~~

Each helper takes optional ISO-8601 ``date_from`` / ``date_to`` strings
(``YYYY-MM-DD``) that are appended verbatim to a parametrised
``BETWEEN`` clause. Empty / ``None`` skips the bound. The filter column
varies by table — ``captured_at`` for screenshots, ``updated_at`` for
notes, ``hour_start`` for hourly cards, ``started_at`` for audio
segments — and is documented in each helper's docstring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger("persona.csv_export")

# Pagination page size. 1 000 rows is the canonical "small enough to fit
# in a single TCP packet after gzip, large enough to amortise the
# round-trip cost" value used elsewhere in the codebase (see
# :mod:`app.web.routes.annotations_ndjson`).
_PAGE_SIZE: int = 1_000

# RFC-4180 line terminator. Same value :mod:`csv` writes by default;
# pinned literally here so the export bytes are identical regardless of
# the host platform's ``os.linesep``.
_CRLF: str = "\r\n"

# Characters that force a field to be quoted. Comma is the delimiter;
# double-quote is the field wrapper; CR / LF would otherwise break the
# record boundary mid-stream.
_QUOTE_TRIGGERS: frozenset[str] = frozenset({",", '"', "\r", "\n"})


def _csv_escape(value: object) -> str:
    """Return ``value`` formatted for a CSV cell, RFC 4180-compliant.

    ``None`` becomes an empty cell. Booleans are coerced via ``int()``
    (so ``True`` → ``"1"``, ``False`` → ``"0"``) — matches how SQLite
    surfaces ``INTEGER`` flag columns and keeps spreadsheet tooling
    happy. Everything else is ``str()``-ified.

    Quoting follows the RFC verbatim: if the rendered text contains a
    comma, a double-quote, ``\\r`` or ``\\n``, the whole field is wrapped
    in double quotes and any internal double-quote is doubled. Cells
    without those triggers are emitted bare — keeping the on-the-wire
    payload as small as possible for big-data tooling that scans the
    file in mmap'd chunks.
    """
    if value is None:
        return ""
    # ``bool`` is a subclass of ``int`` — match it before ``str(value)``
    # would silently render ``"True"`` / ``"False"``. The contract is
    # spelt out: booleans serialise as ``0`` / ``1`` so downstream pandas
    # / DuckDB readers parse them as numeric columns.
    text = ("1" if value else "0") if isinstance(value, bool) else str(value)
    # Fast path — no trigger characters means no quoting needed. The
    # ``any()`` short-circuits as soon as it hits the first trigger, so
    # the common case (plain ASCII identifiers, timestamps, numerics) is
    # a single linear scan with no allocation.
    if not any(ch in _QUOTE_TRIGGERS for ch in text):
        return text
    return '"' + text.replace('"', '""') + '"'


def _csv_row(values: tuple[object, ...]) -> str:
    """Join an iterable of cell values into one CSV record + CRLF."""
    return ",".join(_csv_escape(v) for v in values) + _CRLF


def _normalise_date(value: str | None) -> str | None:
    """Return ``value`` stripped of whitespace, or ``None`` when empty.

    The route layer accepts query strings that may arrive as ``""`` (a
    blank ``<input type="date">``); collapsing those to ``None`` keeps
    the WHERE-clause builder in :func:`_paginated_query` from emitting a
    redundant ``BETWEEN`` against an empty bound that SQLite would
    happily match against every row.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _build_date_filter(
    column: str,
    date_from: str | None,
    date_to: str | None,
) -> tuple[str, list[str]]:
    """Return a parametrised SQL fragment + bind list for the date window.

    ``column`` is the table-specific timestamp column (``captured_at``,
    ``updated_at``, ``hour_start``, ``started_at``). The fragment is
    appended to the seek WHERE clause with a leading ``AND`` so callers
    can compose it unconditionally. Empty bounds produce empty fragments
    (and empty bind lists) so a "no filter" call stays a pure id-seek.

    Bounds are matched against the SQL ``LIKE`` shape ``YYYY-MM-DD%`` —
    inclusive on both ends — instead of a strict ``BETWEEN``. The stored
    columns are full ISO-8601 timestamps (``"2026-06-05T13:42:11Z"``)
    while the UI date pickers return naked ``YYYY-MM-DD`` strings; an
    exact ``BETWEEN '2026-06-05' AND '2026-06-05'`` would miss every
    row from that day because every stored value sorts strictly greater
    than ``"2026-06-05"``. Using ``>=`` for the lower bound and
    ``< (to + 1 day)`` would also work but requires date arithmetic at
    the call site; the half-open ``substr`` form sidesteps that.
    """
    parts: list[str] = []
    params: list[str] = []
    if date_from is not None:
        parts.append(f"{column} >= ?")
        params.append(date_from)
    if date_to is not None:
        # End-of-day sentinel — anything starting with the YYYY-MM-DD
        # prefix is captured by ``< (date_to + "~")`` because ``"~"``
        # sorts strictly after every printable ASCII character SQLite
        # might find in a timestamp suffix (digits, ``T``, ``:``, ``Z``,
        # ``+``, ``-``, ``.``). Cheaper than computing tomorrow's date.
        parts.append(f"{column} < ?")
        params.append(date_to + "~")
    if not parts:
        return "", []
    return " AND " + " AND ".join(parts), params


async def _stream_paginated(
    *,
    header: tuple[str, ...],
    base_sql: str,
    seek_column: str,
    seek_initial: str,
    seek_param_type: type[int] | type[str],
    extra_where: str,
    extra_params: list[str],
    row_formatter: object,
    log_event: str,
) -> AsyncIterator[str]:
    """Yield CSV rows one page at a time using id-seek pagination.

    ``base_sql`` is the ``SELECT … FROM … WHERE`` prefix up to (but not
    including) the seek predicate. The full query becomes::

        {base_sql} {seek_column} > ? {extra_where}
        ORDER BY {seek_column} ASC
        LIMIT {_PAGE_SIZE}

    ``seek_initial`` is the sentinel passed on page 1 (``0`` for INTEGER
    keys, ``""`` for ISO-8601 string keys — both sort below every real
    row). ``row_formatter`` converts a single ``aiosqlite.Row`` into a
    tuple matching ``header`` order. Pagination terminates when a page
    returns fewer than ``_PAGE_SIZE`` rows.

    Holding the connection open across pages would defeat the
    "bounded memory" goal as soon as a slow downstream consumer
    back-pressures the response — Starlette streams synchronously and
    SQLite would happily buffer the full result on the server side. We
    instead open a fresh connection per page; aiosqlite's connection
    pool makes that ~free.
    """
    # Header is emitted exactly once. The route's ``StreamingResponse``
    # turns the AsyncIterator straight into the HTTP body, so this is the
    # first line on the wire.
    yield _csv_row(header)

    last_seek: int | str = seek_initial if seek_param_type is str else 0
    page_index = 0
    total_rows = 0
    while True:
        sql = (
            f"{base_sql} {seek_column} > ?{extra_where} "
            f"ORDER BY {seek_column} ASC LIMIT {_PAGE_SIZE}"
        )
        params: list[object] = [last_seek, *extra_params]
        async with get_connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = list(await cursor.fetchall())
            await cursor.close()
        if not rows:
            break
        for row in rows:
            # ``row_formatter`` is annotated ``object`` to dodge a
            # circular type definition (it returns a tuple of arbitrary
            # SQLite-shaped values); the runtime ``callable()`` check
            # keeps mypy --strict happy when this helper is reused for a
            # fifth table in a future migration.
            assert callable(row_formatter)
            formatted: tuple[object, ...] = row_formatter(row)
            yield _csv_row(formatted)
        total_rows += len(rows)
        page_index += 1
        # Advance the seek cursor to the last row's key. ``aiosqlite`` row
        # objects expose column access by name; the cast keeps mypy --strict
        # happy since the row type widens to ``Any``.
        last_row = rows[-1]
        next_seek = last_row[seek_column]
        last_seek = str(next_seek) if seek_param_type is str else int(next_seek)
        if len(rows) < _PAGE_SIZE:
            break
    log.info(log_event, rows=total_rows, pages=page_index)


def _format_screenshot_row(row: object) -> tuple[object, ...]:
    """Pack a ``screenshots`` row into the public export tuple."""
    # ``aiosqlite.Row`` exposes mapping-style access; the explicit
    # ``int`` / ``str`` coercions match the contract advertised by the
    # column header so a future schema migration that widens a column
    # cannot silently change the CSV's on-disk types.
    return (
        int(row["id"]),  # type: ignore[index]
        str(row["captured_at"]),  # type: ignore[index]
        row["app_name"],  # type: ignore[index]
        row["window_title"],  # type: ignore[index]
        row["ocr_text"],  # type: ignore[index]
        row["pinned_at"],  # type: ignore[index]  # aliased from tier/created_at
        1 if row["alt_text"] is not None else 0,  # type: ignore[index]
    )


async def stream_screenshots_csv(
    date_from: str | None,
    date_to: str | None,
) -> AsyncIterator[str]:
    """Stream every row in ``screenshots`` between the date bounds.

    Columns (in order): ``id``, ``captured_at``, ``app_name``,
    ``window_title``, ``ocr_text``, ``pinned_at``, ``has_alt``.
  ``pinned_at`` is derived from ``tier = 'pinned'`` (there is no
  ``pinned_at`` column in the schema).
    ``has_alt`` is the boolean ``alt_text IS NOT NULL``, surfaced as the
    integer ``1`` / ``0`` so downstream pandas / DuckDB readers parse it
    as a numeric column without extra ``read_csv`` hints.

    The date window filters on ``captured_at`` (UTC ISO-8601). Empty
    bounds disable that half of the window.
    """
    df = _normalise_date(date_from)
    dt = _normalise_date(date_to)
    extra_where, extra_params = _build_date_filter("captured_at", df, dt)
    # ``screenshots.pinned_at`` does not exist: pinning is the ``tier =
    # 'pinned'`` enum flipped in place (see app/pinboard.py). The column was
    # invented by an earlier agent and made this endpoint a hard 500. We keep
    # the public header name and derive the value — ``created_at`` for a
    # pinned shot, empty otherwise — exactly like the pinboard does.
    base_sql = (
        "SELECT id, captured_at, app_name, window_title, ocr_text, "
        "CASE WHEN tier = 'pinned' THEN created_at ELSE '' END AS pinned_at, "
        "alt_text FROM screenshots WHERE"
    )
    log.info("screenshots.csv.stream.start", date_from=df, date_to=dt)
    async for chunk in _stream_paginated(
        header=(
            "id",
            "captured_at",
            "app_name",
            "window_title",
            "ocr_text",
            "pinned_at",
            "has_alt",
        ),
        base_sql=base_sql,
        seek_column="id",
        seek_initial="",  # unused for integer seek; pacifies the union type
        seek_param_type=int,
        extra_where=extra_where,
        extra_params=extra_params,
        row_formatter=_format_screenshot_row,
        log_event="screenshots.csv.stream.done",
    ):
        yield chunk


def _format_note_row(row: object) -> tuple[object, ...]:
    """Pack a ``screenshot_notes`` row into the public export tuple."""
    return (
        int(row["screenshot_id"]),  # type: ignore[index]
        int(row["screenshot_id"]),  # type: ignore[index]
        str(row["body"]),  # type: ignore[index]
        str(row["created_at"]),  # type: ignore[index]
        str(row["updated_at"]),  # type: ignore[index]
    )


async def stream_notes_csv(
    date_from: str | None,
    date_to: str | None,
) -> AsyncIterator[str]:
    """Stream every row in ``screenshot_notes`` between the date bounds.

    Columns: ``id`` (== ``screenshot_id``, the PK), ``screenshot_id``,
    ``body``, ``created_at``, ``updated_at``. The duplicate
    ``id`` / ``screenshot_id`` columns mirror the convention used by the
    other big-data exports (see :mod:`app.web.routes.annotations_csv`
    docstring) so downstream tooling can join on either name. The date
    window filters on ``updated_at`` — last-touch is the most useful
    cursor for incremental syncs.
    """
    df = _normalise_date(date_from)
    dt = _normalise_date(date_to)
    extra_where, extra_params = _build_date_filter("updated_at", df, dt)
    base_sql = (
        "SELECT screenshot_id, body, created_at, updated_at "
        "FROM screenshot_notes WHERE"
    )
    log.info("notes.csv.stream.start", date_from=df, date_to=dt)
    async for chunk in _stream_paginated(
        header=("id", "screenshot_id", "body", "created_at", "updated_at"),
        base_sql=base_sql,
        seek_column="screenshot_id",
        seek_initial="",
        seek_param_type=int,
        extra_where=extra_where,
        extra_params=extra_params,
        row_formatter=_format_note_row,
        log_event="notes.csv.stream.done",
    ):
        yield chunk


def _format_hourly_card_row(row: object) -> tuple[object, ...]:
    """Pack an ``hourly_card`` row into the public export tuple."""
    return (
        str(row["hour_start"]),  # type: ignore[index]
        str(row["hour_end"]),  # type: ignore[index]
        str(row["summary"]),  # type: ignore[index]
        int(row["screen_count"]),  # type: ignore[index]
        int(row["audio_seconds"]),  # type: ignore[index]
        row["top_words"],  # type: ignore[index]
        int(row["llm_enriched"]),  # type: ignore[index]
        str(row["created_at"]),  # type: ignore[index]
    )


async def stream_hourly_cards_csv(
    date_from: str | None,
    date_to: str | None,
) -> AsyncIterator[str]:
    """Stream every row in ``hourly_card`` between the date bounds.

    Columns: ``hour_start``, ``hour_end``, ``summary``, ``screen_count``,
    ``audio_seconds``, ``top_words``, ``llm_enriched``, ``created_at``.
    The natural key ``hour_start`` is a strictly-increasing ISO-8601
    string so it doubles as both the ``ORDER BY`` and the seek cursor;
    no separate ``id`` is needed. The date window filters on the same
    column.
    """
    df = _normalise_date(date_from)
    dt = _normalise_date(date_to)
    extra_where, extra_params = _build_date_filter("hour_start", df, dt)
    base_sql = (
        "SELECT hour_start, hour_end, summary, screen_count, audio_seconds, "
        "top_words, llm_enriched, created_at FROM hourly_card WHERE"
    )
    log.info("hourly_cards.csv.stream.start", date_from=df, date_to=dt)
    async for chunk in _stream_paginated(
        header=(
            "hour_start",
            "hour_end",
            "summary",
            "screen_count",
            "audio_seconds",
            "top_words",
            "llm_enriched",
            "created_at",
        ),
        base_sql=base_sql,
        seek_column="hour_start",
        seek_initial="",
        seek_param_type=str,
        extra_where=extra_where,
        extra_params=extra_params,
        row_formatter=_format_hourly_card_row,
        log_event="hourly_cards.csv.stream.done",
    ):
        yield chunk


def _format_audio_segment_row(row: object) -> tuple[object, ...]:
    """Pack an ``audio_segment`` row into the public export tuple."""
    return (
        int(row["id"]),  # type: ignore[index]
        str(row["started_at"]),  # type: ignore[index]
        str(row["ended_at"]),  # type: ignore[index]
        float(row["duration_s"] or 0.0),  # type: ignore[index]
        str(row["codec"]),  # type: ignore[index]
        row["bitrate"],  # type: ignore[index]
        row["transcript"],  # type: ignore[index]
        row["locale"],  # type: ignore[index]
    )


async def stream_audio_segments_csv(
    date_from: str | None,
    date_to: str | None,
) -> AsyncIterator[str]:
    """Stream every row in ``audio_segment`` between the date bounds.

    Columns: ``id``, ``started_at``, ``ended_at``, ``duration_s``,
    ``codec``, ``bitrate``, ``transcript``, ``locale``. The on-disk
    ``path`` and ``size_bytes`` columns are intentionally excluded —
    they leak the data-dir layout (a privacy footgun) and the size is
    derivable post-export from the transcript length / codec.

    The date window filters on the segment start.

    Schema note — the table's real columns are ``captured_at`` and
    ``duration_seconds``; the ``started_at`` / ``duration_s`` names in the
    CSV header are the *public* export contract and are produced with SQL
    aliases. Selecting the header names directly is what made this endpoint
    a hard 500 ("no such column: started_at") for its entire life.
    """
    df = _normalise_date(date_from)
    dt = _normalise_date(date_to)
    extra_where, extra_params = _build_date_filter("captured_at", df, dt)
    base_sql = (
        "SELECT id, captured_at AS started_at, ended_at, "
        "duration_seconds AS duration_s, codec, bitrate, "
        "transcript, locale FROM audio_segment WHERE"
    )
    log.info("audio_segments.csv.stream.start", date_from=df, date_to=dt)
    async for chunk in _stream_paginated(
        header=(
            "id",
            "started_at",
            "ended_at",
            "duration_s",
            "codec",
            "bitrate",
            "transcript",
            "locale",
        ),
        base_sql=base_sql,
        seek_column="id",
        seek_initial="",
        seek_param_type=int,
        extra_where=extra_where,
        extra_params=extra_params,
        row_formatter=_format_audio_segment_row,
        log_event="audio_segments.csv.stream.done",
    ):
        yield chunk


__all__ = [
    "stream_audio_segments_csv",
    "stream_hourly_cards_csv",
    "stream_notes_csv",
    "stream_screenshots_csv",
]
