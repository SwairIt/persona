"""Personal-metrics dashboard — combined lifetime KPI snapshot.

v1.5 feature 3/3 ships a single read-only surface that rolls up the six
numbers an operator most often wants to brag about (or check up on)
without bouncing between three different stats pages:

* ``lifetime_shots`` — every captured screenshot ever stored.
* ``lifetime_distinct_apps`` — how many distinct foreground apps the
  capture worker has seen (``app_name`` IS NOT NULL).
* ``longest_streak`` — the longest consecutive-day capture run anywhere
  in history. Reused from :mod:`app.streak` so the metric stays in lock
  step with the streak badge on the main dashboard.
* ``total_ocr_chars`` — summed ``LENGTH(ocr_text)`` across every
  screenshot, i.e. the total volume of recognised text Persona is
  sitting on.
* ``total_notes`` — combined count of the two note surfaces: the
  per-screenshot ``screenshot_notes`` rows plus the standalone inbox
  ``notes`` rows. Both are user-authored bodies, so we treat them as a
  single "notes" metric instead of forcing the UI to display two
  near-identical cards.
* ``total_annotations`` — rows in ``screenshot_annotation``, the
  append-only margin scribbles (distinct from ``screenshot_notes`` —
  see the migration header for the rationale).

The function is intentionally a thin, dict-returning surface that
mirrors :mod:`app.embeddings_stats` and :mod:`app.idle_stats` — the
route layer wraps it for HTML and JSON without further reshaping. All
SQL is parametrised (in this module the queries are constant literals
with no user input, but the project standard is "never concatenate
strings into SQL" so we keep the ``execute(sql, params)`` shape even
when ``params`` is empty).

The longest-streak figure is reused via :func:`app.streak.current_streak`
rather than recomputed locally so we have a single source of truth — if
the streak rules ever change (timezones, holiday grace, …) this
dashboard follows along automatically.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.streak import current_streak

log = get_logger("persona.personal_metrics")


class PersonalMetrics(TypedDict):
    """Snapshot returned by :func:`compute_metrics`.

    Every field is a non-negative integer — counts and a character total.
    ``None`` is never returned: an empty database yields zeros, which
    keeps the JSON shape stable for downstream consumers (no ``null``
    branches to guard).
    """

    lifetime_shots: int
    lifetime_distinct_apps: int
    longest_streak: int
    total_ocr_chars: int
    total_notes: int
    total_annotations: int


async def compute_metrics() -> PersonalMetrics:
    """Return the six lifetime KPIs in a single dict.

    A single async ``with get_connection()`` block runs every query so
    the snapshot is taken against the same SQLite reader transaction —
    no risk of a write landing between, say, the shots count and the
    OCR total and making the numbers internally inconsistent.

    ``COALESCE(SUM(...), 0)`` guards the OCR total so an empty
    ``screenshots`` table returns ``0`` rather than ``None`` (SQLite's
    ``SUM`` over zero rows is ``NULL``).
    """
    streak_payload = await current_streak()
    longest_streak = int(streak_payload["longest"])

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots", ())
        shots_row = await cursor.fetchone()
        lifetime_shots = int(shots_row["n"]) if shots_row is not None else 0

        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT app_name) AS n FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name <> ''",
            (),
        )
        apps_row = await cursor.fetchone()
        lifetime_distinct_apps = int(apps_row["n"]) if apps_row is not None else 0

        cursor = await conn.execute(
            "SELECT COALESCE(SUM(LENGTH(ocr_text)), 0) AS n FROM screenshots "
            "WHERE ocr_text IS NOT NULL",
            (),
        )
        ocr_row = await cursor.fetchone()
        total_ocr_chars = int(ocr_row["n"]) if ocr_row is not None else 0

        # "Notes" is the union of the two user-authored note surfaces:
        # per-screenshot notes (``screenshot_notes``) and the standalone
        # inbox (``notes``). Counted with two separate queries rather
        # than a UNION ALL so the planner can hit each table's PK index
        # directly without materialising a temp result.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshot_notes", ()
        )
        screenshot_notes_row = await cursor.fetchone()
        screenshot_notes_count = (
            int(screenshot_notes_row["n"]) if screenshot_notes_row is not None else 0
        )

        cursor = await conn.execute("SELECT COUNT(*) AS n FROM notes", ())
        inbox_notes_row = await cursor.fetchone()
        inbox_notes_count = (
            int(inbox_notes_row["n"]) if inbox_notes_row is not None else 0
        )

        total_notes = screenshot_notes_count + inbox_notes_count

        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshot_annotation", ()
        )
        annotations_row = await cursor.fetchone()
        total_annotations = (
            int(annotations_row["n"]) if annotations_row is not None else 0
        )

    log.info(
        "personal_metrics.computed",
        lifetime_shots=lifetime_shots,
        lifetime_distinct_apps=lifetime_distinct_apps,
        longest_streak=longest_streak,
        total_ocr_chars=total_ocr_chars,
        total_notes=total_notes,
        screenshot_notes_count=screenshot_notes_count,
        inbox_notes_count=inbox_notes_count,
        total_annotations=total_annotations,
    )

    return PersonalMetrics(
        lifetime_shots=lifetime_shots,
        lifetime_distinct_apps=lifetime_distinct_apps,
        longest_streak=longest_streak,
        total_ocr_chars=total_ocr_chars,
        total_notes=total_notes,
        total_annotations=total_annotations,
    )
