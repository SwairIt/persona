"""HTTP route for the v0.79 per-day kanban CSV export.

``GET /export/kanban.csv?day=YYYY-MM-DD`` streams a ``text/csv`` document
covering the same screenshots the v0.46 JSON sibling at
``/api/kanban/{day}.json`` exposes, flattened to one row per shot. The
JSON view groups shots into per-app columns; for CSV consumers (Excel,
pandas, ``cut``) the per-app grouping is purely cosmetic, so we collapse
the structure to columns:

    app_name, shot_id, captured_at

* ``app_name``    — the ``screenshots.app_name`` string. NULL / empty
                    is normalised to ``"Unknown"`` to match the JSON
                    bucket label produced by :mod:`app.web.routes.day_kanban`.
* ``shot_id``     — integer FK back to ``screenshots.id``.
* ``captured_at`` — ISO-8601 UTC timestamp (re-parsed and re-serialised
                    via :mod:`app.storage.time` so the format matches
                    what the JSON endpoint emits, not whatever raw shape
                    SQLite happened to store).

Row order mirrors the JSON: columns sorted by shot-count descending
(ties break case-insensitively by ``app_name``), and inside each column
shots are newest-first by ``captured_at``. Keeping the order identical
across the two formats means a user diffing the JSON against the CSV
sees the same sequence — diagnostic gold when something looks wrong.

The endpoint streams via :class:`fastapi.responses.StreamingResponse` in
a single in-memory chunk — same shape as :mod:`app.web.routes.stats_csv`
and :mod:`app.web.routes.share_visits_csv`. A single calendar day even
on a heavy-capture machine is well below the ``_MAX_SHOTS_PER_DAY`` cap
borrowed from the JSON endpoint, so the all-at-once approach keeps the
``Content-Length`` header authoritative for download-progress UI.

The ``day`` query parameter accepts ``YYYY-MM-DD``. Unlike the
forgiving HTML/JSON kanban routes (which silently fall back to *today*
on a typo), CSV consumers are typically scripts where a silent fallback
masks bugs — so a malformed ``day`` here returns ``400`` instead. An
omitted ``day`` still defaults to today, matching what a human visiting
the URL in a browser expects.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso as _iso
from app.storage.time import parse_iso as _parse_iso

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.kanban_csv")

router = APIRouter(prefix="/export", tags=["kanban-csv"])

# Hard ceiling on shots emitted in a single CSV. Matches the JSON sibling
# (``app.web.routes.day_kanban._MAX_SHOTS_PER_DAY``) so the two formats
# agree byte-for-byte on which screenshots are included. Bumping the cap
# here without bumping it there would silently desync the two views.
_MAX_SHOTS_PER_DAY = 5_000

# Bucket label for screenshots whose ``app_name`` is NULL or empty —
# kept in lockstep with :mod:`app.web.routes.day_kanban` so users see
# the same string in the HTML, the JSON, and the CSV.
_UNKNOWN_APP_LABEL = "Unknown"

_CSV_COLUMNS: tuple[str, ...] = ("app_name", "shot_id", "captured_at")


def _today_local() -> date:
    """Local-date "today" — matches the wall clock and the JSON sibling."""
    return datetime.now().astimezone().date()


def _parse_day_or_400(day: str | None) -> date:
    """Parse ``YYYY-MM-DD`` or raise ``HTTPException(400)``.

    Diverges from :func:`app.web.routes.day_kanban._parse_day_or_today`
    on purpose. CSV is a machine-consumer surface — silently swapping a
    typo for "today" would hide bugs in callers' date-handling code.
    An absent ``day`` *is* allowed to fall through to today, because a
    human poking at ``/export/kanban.csv`` in a browser still expects
    "something useful".
    """
    if day is None or day == "":
        return _today_local()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError as exc:
        log.info("kanban_csv.day_invalid", value=day)
        raise HTTPException(
            status_code=400,
            detail="day must be formatted as YYYY-MM-DD",
        ) from exc


def _day_bounds_utc(day_value: date) -> tuple[datetime, datetime]:
    """Translate a local calendar day to half-open ``[since_utc, until_utc)``.

    Mirrors :func:`app.web.routes.day_kanban._day_bounds_utc` — the two
    routes have to agree on the day window, or a shot captured at 23:59
    local could appear in one format and not the other.
    """
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(day_value.year, day_value.month, day_value.day, tzinfo=tz)
    until_local = since_local + timedelta(days=1)
    return since_local.astimezone(UTC), until_local.astimezone(UTC)


async def _render_kanban_csv(*, day_value: date) -> str:
    """Read the day's screenshots and return the CSV body as a string.

    Split out from the route so future callers (a CLI subcommand, a
    smoke test) can reuse the exact same query + serialisation without
    spinning up FastAPI. The function is also the natural unit-test seam.

    Parametrised SQL only — the day bounds flow in as bound ``?``
    placeholders so no future contributor can accidentally land a
    string-concat SQL bug here.
    """
    since_dt, until_dt = _day_bounds_utc(day_value)

    # Materialise rows into per-app buckets so we can reproduce the
    # JSON's column ordering (count desc, app_name asc) before writing
    # them out. A flat ``ORDER BY captured_at DESC`` would emit a
    # different sequence than the JSON, which silently desyncs the two
    # representations of the same data.
    grouped: dict[str, list[tuple[int, str]]] = {}

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, app_name
            FROM screenshots
            WHERE captured_at >= ?
              AND captured_at < ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (_iso(since_dt), _iso(until_dt), _MAX_SHOTS_PER_DAY),
        )
        async for row in cursor:
            raw_name = row["app_name"]
            has_name = raw_name is not None and str(raw_name).strip() != ""
            app_name = str(raw_name) if has_name else _UNKNOWN_APP_LABEL

            # Re-parse + re-serialise the timestamp via the storage
            # helpers so the CSV's ``captured_at`` formatting matches
            # the JSON sibling exactly (UTC, ISO-8601 with offset). If
            # the stored value is corrupt skip the row rather than nuke
            # the whole export.
            try:
                captured_at_dt = _parse_iso(str(row["captured_at"]))
            except ValueError:
                log.warning(
                    "kanban_csv.row_skip_bad_timestamp",
                    shot_id=int(row["id"]) if row["id"] is not None else -1,
                )
                continue

            grouped.setdefault(app_name, []).append((int(row["id"]), _iso(captured_at_dt)))

    # Order columns identically to the JSON: count desc, then app_name
    # ascending case-insensitively for tie-break stability.
    ordered_apps = sorted(
        grouped.items(),
        key=lambda kv: (-len(kv[1]), kv[0].casefold()),
    )

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(_CSV_COLUMNS)

    rows_written = 0
    for app_name, shots in ordered_apps:
        # ``shots`` are already newest-first thanks to the SQL ORDER BY;
        # within a column we keep that order, again matching the JSON.
        for shot_id, captured_at_iso in shots:
            writer.writerow((app_name, shot_id, captured_at_iso))
            rows_written += 1

    log.info(
        "kanban_csv.render.ok",
        day=day_value.isoformat(),
        columns=len(ordered_apps),
        rows=rows_written,
    )
    return buffer.getvalue()


@router.get("/kanban.csv", response_model=None)
async def export_kanban_csv(
    day: str | None = Query(default=None),
) -> StreamingResponse:
    """Stream the per-day kanban data as CSV (``app_name, shot_id, captured_at``)."""
    day_value = _parse_day_or_400(day)

    try:
        body = await _render_kanban_csv(day_value=day_value)
    except HTTPException:
        # Bubble 4xx straight through — they're already structured.
        raise
    except Exception:
        log.exception("kanban_csv.route.failed", day=day_value.isoformat())
        raise HTTPException(
            status_code=500,
            detail="kanban CSV export failed",
        ) from None

    payload = body.encode("utf-8")
    filename = f"persona-kanban-{day_value.isoformat()}.csv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info(
        "kanban_csv.route.ok",
        day=day_value.isoformat(),
        bytes=len(payload),
    )

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
