"""Per-day timeline JSON — full machine-readable export of one day.

v0.93 feature 1/3. A single endpoint, ``GET /api/day/{day_iso}.json``,
materialises four sibling tables for one local calendar day into a flat
JSON document that downstream automation (CLI exports, journaling
integrations, future SPA, third-party readers) can consume without
scraping any HTML view:

* ``shots``       — one row per :sql:`screenshots` row captured that day,
  projected as ``{id, captured_at, app_name, ocr_text}``. The OCR text is
  fed through :func:`app.redaction.apply_redaction` so masked tokens
  (emails, card numbers, bearer secrets) never leak out via the JSON
  export — the same policy that protects the searchable FTS index also
  protects this machine-readable surface.
* ``notes``       — standalone inbox-style notes from the :sql:`notes`
  table, projected as ``{id, body, created_at}``. Encrypted notes
  surface with an empty body and a ``[locked]`` marker so callers see
  *that* a row exists without ever receiving the ciphertext.
* ``annotations`` — per-shot append-only commentary lines from the
  :sql:`screenshot_annotation` table, projected as ``{shot_id, body}``.
  The column is named ``screenshot_id`` in the table; we alias to
  ``shot_id`` in the JSON so the four collections share one vocabulary.
* ``stickies``    — per-shot overlay scribbles from the :sql:`sticky_note`
  table, projected as ``{shot_id, x_pct, y_pct, body}``.

The ``day_iso`` path component must be ``YYYY-MM-DD``; anything else
returns a 400 with a one-line message. Unlike the day scrubber (which
silently falls back to "today" on a typo) this is a machine surface, so
the strict contract is the friendlier behaviour — a script that sent a
malformed string wants to *know* rather than get yesterday's data.

The four queries fan out across two tables that are *not* keyed on a
day column (annotations + stickies are keyed by ``screenshot_id``), so
we resolve the day boundary against ``screenshots.captured_at`` once,
collect the matching shot ids, and join via an ``IN (...)`` parametrised
clause. All SQL placeholders are ``?`` — no string interpolation of
user data, which would trip ``ruff S608`` and is what a careless
implementation gets wrong.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``. Wire
it up in a follow-up patch with::

    from app.web.routes import day_json as day_json_routes
    app.include_router(day_json_routes.router)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.day_json")

router = APIRouter(tags=["day-json"])

# Hard ceilings per collection. A captured-every-second day still fits
# well under the shot cap; the smaller caps on notes/annotations/
# stickies reflect the fact that those are human-typed and a four-digit
# count per day already signals automation gone wrong.
_MAX_SHOTS_PER_DAY: Final[int] = 5_000
_MAX_NOTES_PER_DAY: Final[int] = 1_000
_MAX_ANNOTATIONS_PER_DAY: Final[int] = 5_000
_MAX_STICKIES_PER_DAY: Final[int] = 5_000

# Same sentinel as :mod:`app.web.routes.notes_timeline` and
# :mod:`app.web.routes.notes` so HTML, JSON, CLI, and TUI clients all
# surface the *exact* same literal for encrypted notes.
LOCKED_MARKER: Final[str] = "[locked]"


# ---------------------------------------------------------------------------
# Day-boundary helpers
# ---------------------------------------------------------------------------


def _validate_day_iso(day_iso: str) -> str:
    """Strictly parse ``YYYY-MM-DD``; return the canonical ISO string.

    A machine endpoint owes its caller a loud failure on bad input — we
    deliberately do *not* fall back to "today" the way the exploratory
    HTML views do. The 400 carries a one-line hint so a script author
    can fix the call without spelunking through logs.
    """
    try:
        parsed = datetime.strptime(day_iso, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="day_iso must be in YYYY-MM-DD form",
        ) from exc
    return parsed.isoformat()


def _day_bounds_utc(day_iso: str) -> tuple[str, str]:
    """Translate a local ``YYYY-MM-DD`` into the half-open UTC window
    ``[since, until)`` as ISO strings — the format ``screenshots.captured_at``
    is stored in.

    We do the conversion at the local-tz boundary (midnight to midnight
    on the user's wall clock) rather than at UTC midnight so that the
    JSON export agrees with what the user sees in the day-scrubber and
    every other day-keyed view in the app.
    """
    parsed = datetime.strptime(day_iso, "%Y-%m-%d").date()
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(parsed.year, parsed.month, parsed.day, tzinfo=tz)
    until_local = since_local + timedelta(days=1)
    return (
        since_local.astimezone(UTC).isoformat(),
        until_local.astimezone(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Per-collection loaders
# ---------------------------------------------------------------------------


_SELECT_SHOTS = (
    "SELECT id, captured_at, app_name, ocr_text "
    "FROM screenshots "
    "WHERE captured_at >= ? AND captured_at < ? "
    "ORDER BY captured_at ASC, id ASC "
    "LIMIT ?"
)


async def _load_shots(
    conn: aiosqlite.Connection,
    since_iso: str,
    until_iso: str,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Fetch the day's screenshots and run OCR text through redaction.

    Returns ``(items, shot_ids)``: the JSON-ready dicts and the list of
    primary keys, the latter used by the annotation + sticky queries so
    they only consider rows that belong to this day's shots.
    """
    cursor = await conn.execute(
        _SELECT_SHOTS,
        (since_iso, until_iso, _MAX_SHOTS_PER_DAY),
    )
    rows = await cursor.fetchall()

    items: list[dict[str, Any]] = []
    shot_ids: list[int] = []
    for row in rows:
        sid = int(row["id"])
        shot_ids.append(sid)
        raw_ocr = row["ocr_text"]
        ocr_text = str(raw_ocr) if raw_ocr is not None else ""
        masked, _count = await apply_redaction(ocr_text)
        items.append(
            {
                "id": sid,
                "captured_at": str(row["captured_at"]),
                "app_name": (
                    str(row["app_name"]) if row["app_name"] is not None else None
                ),
                "ocr_text": masked,
            }
        )
    return items, shot_ids


_SELECT_NOTES = (
    "SELECT id, body, created_at, encrypted "
    "FROM notes "
    "WHERE date(created_at) = ? "
    "ORDER BY created_at ASC, id ASC "
    "LIMIT ?"
)


async def _load_notes(
    conn: aiosqlite.Connection,
    day_iso: str,
) -> list[dict[str, Any]]:
    """Fetch standalone notes whose local-day matches ``day_iso``.

    Notes are stored with ``datetime('now')`` which is SQLite UTC; we
    filter via ``date(created_at) = ?`` for symmetry with
    :mod:`app.web.routes.notes_timeline`. Encrypted rows surface with a
    blank body and the ``[locked]`` marker so the JSON shape stays
    uniform without ever serialising ciphertext.
    """
    cursor = await conn.execute(
        _SELECT_NOTES,
        (day_iso, _MAX_NOTES_PER_DAY),
    )
    rows = await cursor.fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        is_encrypted = bool(int(row["encrypted"] or 0))
        body = "" if is_encrypted else str(row["body"])
        item: dict[str, Any] = {
            "id": int(row["id"]),
            "body": body,
            "created_at": str(row["created_at"]),
        }
        if is_encrypted:
            item["marker"] = LOCKED_MARKER
        items.append(item)
    return items


async def _load_annotations(
    conn: aiosqlite.Connection,
    shot_ids: list[int],
) -> list[dict[str, Any]]:
    """Fetch every annotation attached to one of ``shot_ids``.

    The table column is ``screenshot_id`` but the JSON contract calls it
    ``shot_id`` (consistent with the sticky-note projection). We alias
    in SQL with ``AS shot_id`` so the row-mapping loop reads cleanly.

    Empty ``shot_ids`` short-circuits — an ``IN ()`` with no values is a
    SQLite syntax error.
    """
    if not shot_ids:
        return []
    placeholders = ",".join("?" * len(shot_ids))
    cursor = await conn.execute(
        f"SELECT screenshot_id AS shot_id, body "  # noqa: S608 - placeholders are only "?"
        f"FROM screenshot_annotation "
        f"WHERE screenshot_id IN ({placeholders}) "
        f"ORDER BY created_at ASC, id ASC "
        f"LIMIT ?",
        (*shot_ids, _MAX_ANNOTATIONS_PER_DAY),
    )
    rows = await cursor.fetchall()
    return [
        {
            "shot_id": int(row["shot_id"]),
            "body": str(row["body"]),
        }
        for row in rows
    ]


async def _load_stickies(
    conn: aiosqlite.Connection,
    shot_ids: list[int],
) -> list[dict[str, Any]]:
    """Fetch every sticky-note overlay attached to one of ``shot_ids``.

    The sticky table already names the column ``shot_id``, so no alias
    is needed; the projection mirrors :mod:`app.web.routes.sticky_export`
    minus the metadata fields (color, created_at) the task spec omits.
    """
    if not shot_ids:
        return []
    placeholders = ",".join("?" * len(shot_ids))
    cursor = await conn.execute(
        f"SELECT shot_id, x_pct, y_pct, body "  # noqa: S608 - placeholders are only "?"
        f"FROM sticky_note "
        f"WHERE shot_id IN ({placeholders}) "
        f"ORDER BY created_at ASC, id ASC "
        f"LIMIT ?",
        (*shot_ids, _MAX_STICKIES_PER_DAY),
    )
    rows = await cursor.fetchall()
    return [
        {
            "shot_id": int(row["shot_id"]),
            "x_pct": float(row["x_pct"]),
            "y_pct": float(row["y_pct"]),
            "body": str(row["body"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/api/day/{day_iso}.json", response_class=JSONResponse)
async def day_json(day_iso: str) -> JSONResponse:
    """Return the full machine-readable timeline for one local day.

    Response shape::

        {
          "day": "YYYY-MM-DD",
          "shots":       [{id, captured_at, app_name, ocr_text}, ...],
          "notes":       [{id, body, created_at}, ...],
          "annotations": [{shot_id, body}, ...],
          "stickies":    [{shot_id, x_pct, y_pct, body}, ...]
        }

    Encrypted notes carry an extra ``marker == "[locked]"`` field and a
    blanked ``body``; ciphertext is never serialised. Bad ``day_iso``
    yields a 400 — see :func:`_validate_day_iso`.
    """
    canonical = _validate_day_iso(day_iso)
    since_iso, until_iso = _day_bounds_utc(canonical)

    async with get_connection() as conn:
        shots, shot_ids = await _load_shots(conn, since_iso, until_iso)
        notes = await _load_notes(conn, canonical)
        annotations = await _load_annotations(conn, shot_ids)
        stickies = await _load_stickies(conn, shot_ids)

    log.info(
        "day_json.export",
        day=canonical,
        shots=len(shots),
        notes=len(notes),
        annotations=len(annotations),
        stickies=len(stickies),
    )

    return JSONResponse(
        {
            "day": canonical,
            "shots": shots,
            "notes": notes,
            "annotations": annotations,
            "stickies": stickies,
        }
    )


__all__ = ["LOCKED_MARKER", "router"]
