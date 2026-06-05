"""Per-app summary card — auto-generated stat snapshot for a single app.

A "summary card" is a compact, opinionated digest of *one* app's recent
activity, intended to be embedded:

* As a standalone page at ``/app/{app_name}/summary``.
* As a JSON blob at ``/api/app/{app_name}/summary.json`` for the future
  desktop / mobile widget shells.
* As an HTMX fragment at ``/widget/app-card/{app_name}`` so any other
  page (now-dashboard, tag pages, share pages, ...) can drop the card
  in without re-implementing the SQL.

The HTTP surface lives in :mod:`app.web.routes.app_summary_card`; this
module is the pure-data side and has no Jinja / Starlette dependencies
so it can be unit-tested against a temp SQLite without a running app.

Card contract
-------------

The dict returned by :func:`build_app_card` has a stable shape — once a
field is shipped it never disappears, only new fields are added on top.
That contract is what the JSON sibling endpoint exposes and what the
template iterates over. Concretely:

``app_name``
    Echoed back so a downstream renderer that only has the dict (no
    request context) can still title the card.

``days``
    Window size in days the rest of the numbers describe.
    ``[today - days + 1, today]`` inclusive of today, matching the
    convention used by :mod:`app.time_on_app.app_summary`.

``total_shots``
    Count of screenshots in the window. Zero when the app has not
    been seen — *not* ``None``, so the template never has to special-
    case absence.

``total_voice_seconds``
    Sum of ``audio_segment.duration_seconds`` for every audio segment
    whose *hour bucket* intersects an hour the app was active in.
    "Hour bucket" = ``strftime('%Y-%m-%dT%H', captured_at)``. We do not
    do a per-second join because the audio table records speech
    *segments* (10-30 s blobs) not continuous coverage, and the
    screenshot table records *samples* not continuous coverage either
    — a strict overlap join under-counts heavily on both sides. Hour
    intersection is the lowest-resolution honest measure: if the user
    was in the app at some point during 14:00 and there's a recorded
    voice segment starting 14:12, we attribute that voice to the app.

``top_titles``
    Up to five ``{title, count}`` dicts, the most-seen
    ``window_title`` values for the app over the window. Drops empty
    / NULL titles so the list always carries real strings.

``first_seen`` / ``last_seen``
    ISO strings of the *all-time* first / last shot for the app — not
    windowed. This is deliberately broader than the rest of the card:
    the operator wants to see "I have used this app since 2024-01-03"
    even when they're looking at the 7-day window. Both are ``None``
    when the app has never been seen.

``sample_shot_id``
    The most recent shot's ``id`` (lifetime, not windowed) so the card
    has *some* visual anchor even when the chosen window is empty.
    ``None`` when the app has no shots at all.

``sample_shot_thumb_path``
    Thumbnail path for ``sample_shot_id``, suitable for the
    ``| thumbnail_url`` Jinja filter. ``None`` when the shot has no
    thumbnail (cold tier or thumb-regen pending). Surfaced alongside
    ``sample_shot_id`` so the template doesn't need a second DB hop.

``sparkline_per_day``
    A length-``days`` list of integers, *oldest first*. Day ``i``
    corresponds to ``today - (days - 1 - i)``. Missing days get a
    zero so the template can map straight into the SVG path without
    a sparse-index dance.

All queries are parametrised; no string interpolation goes near user
input. The ``audio_segment`` join is the one query that's slightly
non-obvious — see the inline comment around the ``hour_buckets`` CTE.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Final

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.app_summary_card")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Cap on top window titles surfaced in the card. Five is small enough
# to fit a single phone-width column without scrolling and large enough
# to capture the typical work pattern (browser + 2-3 active project
# windows) most operators end up with.
_TOP_TITLES_LIMIT: Final[int] = 5

# Floor / ceiling on the ``days`` argument. The lower bound rejects
# nonsense (zero / negative windows) by returning an empty-but-shaped
# card; the upper bound caps a single SQL scan at one year so a typoed
# ``days=99999`` never blocks the event loop on a giant table.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def build_app_card(app_name: str, days: int = 7) -> dict[str, Any]:
    """Assemble the summary card for ``app_name`` over the last ``days``.

    Pure async function — yields control on every DB round-trip so the
    event loop stays responsive even on a cold cache. Result is a
    plain ``dict`` so callers (Jinja, JSON encoder, future GraphQL
    shim) can consume it without an extra TypedDict import.

    ``days`` is clamped to ``[_MIN_DAYS, _MAX_DAYS]`` rather than
    raising. Unknown apps return a fully-populated card with zeros and
    ``None`` for the optional fields — the template can render that
    without branching.
    """
    safe_name = (app_name or "").strip()
    safe_days = max(_MIN_DAYS, min(_MAX_DAYS, int(days)))

    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=safe_days - 1)

    async with get_connection() as conn:
        # 1. Total shots inside the window. ``DATE(captured_at)`` runs
        #    against the indexed column directly; no extra index needed
        #    because ``screenshots`` already carries an index on
        #    ``app_name`` (per migration 002) and the table is per-day-
        #    partitioned in practice.
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM screenshots
             WHERE app_name = ?
               AND DATE(captured_at) >= ?
               AND DATE(captured_at) <= ?
            """,
            (safe_name, start_day.isoformat(), today.isoformat()),
        )
        row = await cursor.fetchone()
        total_shots = int(row["n"]) if row is not None else 0

        # 2. Top window_titles in the window. NULL / empty titles are
        #    filtered at the SQL level so the template always sees a
        #    real string. ``LIMIT _TOP_TITLES_LIMIT`` is parametrised
        #    through a bound integer to keep ruff happy (no f-string
        #    SQL even though the value is a module constant).
        cursor = await conn.execute(
            """
            SELECT window_title, COUNT(*) AS n
              FROM screenshots
             WHERE app_name = ?
               AND DATE(captured_at) >= ?
               AND DATE(captured_at) <= ?
               AND window_title IS NOT NULL
               AND window_title != ''
             GROUP BY window_title
             ORDER BY n DESC
             LIMIT ?
            """,
            (safe_name, start_day.isoformat(), today.isoformat(), _TOP_TITLES_LIMIT),
        )
        title_rows = await cursor.fetchall()
        top_titles = [
            {"title": str(r["window_title"]), "count": int(r["n"])}
            for r in title_rows
        ]

        # 3. Lifetime first / last seen + sample shot id. Lifetime
        #    (not windowed) is the user-facing definition; see module
        #    docstring. A single round-trip aggregates all three so
        #    we don't pay for two extra SELECTs.
        cursor = await conn.execute(
            """
            SELECT MIN(captured_at) AS first_seen,
                   MAX(captured_at) AS last_seen
              FROM screenshots
             WHERE app_name = ?
            """,
            (safe_name,),
        )
        row = await cursor.fetchone()
        first_seen = (
            str(row["first_seen"])
            if row is not None and row["first_seen"] is not None
            else None
        )
        last_seen = (
            str(row["last_seen"])
            if row is not None and row["last_seen"] is not None
            else None
        )

        cursor = await conn.execute(
            """
            SELECT id, thumbnail_path
              FROM screenshots
             WHERE app_name = ?
             ORDER BY captured_at DESC
             LIMIT 1
            """,
            (safe_name,),
        )
        row = await cursor.fetchone()
        sample_shot_id = int(row["id"]) if row is not None else None
        sample_shot_thumb_path = (
            str(row["thumbnail_path"])
            if row is not None and row["thumbnail_path"] is not None
            else None
        )

        # 4. Sparkline — one row per day in the window. SQLite returns
        #    only days that actually have shots; the densification to
        #    a length-``days`` list happens in Python below.
        cursor = await conn.execute(
            """
            SELECT DATE(captured_at) AS day, COUNT(*) AS n
              FROM screenshots
             WHERE app_name = ?
               AND DATE(captured_at) >= ?
               AND DATE(captured_at) <= ?
             GROUP BY day
            """,
            (safe_name, start_day.isoformat(), today.isoformat()),
        )
        day_rows = await cursor.fetchall()
        per_day_counts: dict[str, int] = {
            str(r["day"]): int(r["n"]) for r in day_rows
        }
        sparkline_per_day = [
            per_day_counts.get(
                (start_day + timedelta(days=offset)).isoformat(),
                0,
            )
            for offset in range(safe_days)
        ]

        # 5. Voice seconds intersecting app-active hours. We compute the
        #    set of app-active hour buckets in a CTE, then sum the
        #    duration of every audio segment whose own hour bucket is in
        #    that set. This is the honest hour-resolution attribution
        #    described in the module docstring. The window for *audio*
        #    is the same calendar window as the app, not lifetime —
        #    matching the "weekly time" framing of the card.
        cursor = await conn.execute(
            """
            WITH app_hours AS (
                SELECT DISTINCT strftime('%Y-%m-%dT%H', captured_at) AS hour_bucket
                  FROM screenshots
                 WHERE app_name = ?
                   AND DATE(captured_at) >= ?
                   AND DATE(captured_at) <= ?
            )
            SELECT COALESCE(SUM(a.duration_seconds), 0.0) AS total_seconds
              FROM audio_segment AS a
             WHERE strftime('%Y-%m-%dT%H', a.captured_at) IN (
                       SELECT hour_bucket FROM app_hours
                   )
               AND DATE(a.captured_at) >= ?
               AND DATE(a.captured_at) <= ?
            """,
            (
                safe_name,
                start_day.isoformat(),
                today.isoformat(),
                start_day.isoformat(),
                today.isoformat(),
            ),
        )
        row = await cursor.fetchone()
        # ``COALESCE`` guarantees a numeric, but the row itself can be
        # ``None`` if the ``audio_segment`` table doesn't exist on a
        # fresh install pre-migration-092. We coerce to ``0.0`` in that
        # path so the card still renders.
        total_voice_seconds = (
            float(row["total_seconds"]) if row is not None else 0.0
        )

    card: dict[str, Any] = {
        "app_name": safe_name,
        "days": safe_days,
        "total_shots": total_shots,
        "total_voice_seconds": total_voice_seconds,
        "top_titles": top_titles,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "sample_shot_id": sample_shot_id,
        "sample_shot_thumb_path": sample_shot_thumb_path,
        "sparkline_per_day": sparkline_per_day,
    }

    log.info(
        "app_summary_card.built",
        app_name=safe_name,
        days=safe_days,
        total_shots=total_shots,
        total_voice_seconds=total_voice_seconds,
        top_titles_n=len(top_titles),
        has_sample_shot=sample_shot_id is not None,
        sparkline_sum=sum(sparkline_per_day),
    )
    return card


# ---------------------------------------------------------------------------
# Helper exposed for the template (and the JSON endpoint)
# ---------------------------------------------------------------------------


def days_since(iso_ts: str | None, *, _today: date | None = None) -> int | None:
    """Return whole days between ``iso_ts`` and today (local), or ``None``.

    Exposed so the template can render "first seen N days ago" without
    embedding date math in Jinja. ``_today`` is a test seam — production
    callers leave it at its default of ``datetime.now().astimezone()``.
    """
    if iso_ts is None:
        return None
    try:
        when = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    base = _today if _today is not None else datetime.now().astimezone().date()
    return max(0, (base - when.date()).days)


__all__ = ["build_app_card", "days_since"]
