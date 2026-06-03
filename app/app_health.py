"""Per-app health aggregation — last-seen, recent volume, OCR failure rate.

Persona v0.69 feature 1/3.

Powers the ``/stats/app-health`` dashboard. Walks the ``screenshots``
table once per call and folds it into a small ``app_name``-keyed
summary with three numbers per row:

* ``last_seen``     — most recent ``captured_at`` seen for the app (ever).
* ``shots_7d``      — captures in the last ``days`` UTC days (default 7).
* ``ocr_fail_rate_pct`` — share of completed OCR jobs whose
  ``ocr_status = 'failed'`` over the same window, rounded to one
  decimal place. ``0.0`` when no completed OCR happened for the app in
  the window — the dashboard layer then renders that as a neutral
  "no data" cell rather than a green "all good" cell.

A single SQL pass groups by ``app_name``; SQLite's conditional
``SUM(CASE…)`` form is used to count the failure / completed subsets
without a second scan. Rows with ``app_name IS NULL`` (capture taken
before any window was focused, or in a sandbox the detector couldn't
attribute) are dropped — they'd render as an unhelpful blank row on
the dashboard.

The function is sync-friendly: it issues one parametrised query, awaits
the cursor, projects each row into a ``TypedDict`` and returns the
list sorted by ``shots_7d`` descending so the busiest app pops to the
top. ``last_seen`` ties are broken alphabetically for determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.app_health")

# Hard bounds on the look-back window. Mirrors the convention used by
# ``app.ocr.error_rate`` — a stray query-string can never trigger a
# full-history scan.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 7


class AppHealthRow(TypedDict):
    """One app's row in the health dashboard payload.

    Attributes:
        app_name:           ``screenshots.app_name`` verbatim — never
                            ``None`` because the SQL filters those out.
        last_seen:          ISO timestamp of the most recent capture
                            ever recorded for the app. May fall outside
                            the ``days`` window; that's intentional, it
                            tells the operator the app is dormant.
        shots_7d:           Captures (any ``ocr_status``) inside the
                            window. Named ``_7d`` for compatibility with
                            the spec even when ``days != 7``.
        ocr_fail_rate_pct:  ``failed / (failed + done) * 100`` over the
                            window, rounded to one decimal. ``0.0``
                            when neither bucket is populated.
    """

    app_name: str
    last_seen: str
    shots_7d: int
    ocr_fail_rate_pct: float


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


async def compute_app_health(days: int = _DEFAULT_DAYS) -> list[AppHealthRow]:
    """Aggregate per-app health metrics over the last ``days`` days.

    The query joins three numbers per app in one pass:

    * ``MAX(captured_at)`` — most-recent capture across all history, so
      a long-dormant app still surfaces with a stale ``last_seen``.
    * ``SUM(captured_at >= cutoff)`` — captures inside the window.
    * Conditional ``SUM`` of ``ocr_status = 'failed'`` and ``'done'`` —
      the failure rate is then ``failed / (failed + done) * 100``,
      ignoring ``pending`` / ``skipped`` rows so the metric tracks OCR
      *quality* rather than "work not done yet" / "intentionally
      excluded".

    Args:
        days: Look-back window in days. Clamped to ``[1, 365]``.

    Returns:
        A list of :class:`AppHealthRow`, sorted by ``shots_7d``
        descending, ties broken by ``app_name`` ascending. Apps with
        zero captures in the window are still included — they're
        surfaced in grey on the dashboard so the operator notices
        regressions ("this app used to be busy, now it's silent").
    """
    window = _clamp_days(days)

    cutoff = datetime.now(UTC) - timedelta(days=window)
    cutoff_iso = iso(cutoff)

    rows: list[AppHealthRow] = []

    async with get_connection() as conn:
        # One pass, grouped by app_name. Conditional SUMs let us derive
        # window counts and the failed/done split without a second
        # query. ``captured_at >= ?`` uses the existing
        # ``idx_screenshots_captured_at`` index for the predicate; the
        # aggregate fans out per group from there.
        cursor = await conn.execute(
            "SELECT app_name AS app_name, "
            "       MAX(captured_at) AS last_seen, "
            "       SUM(CASE WHEN captured_at >= ? THEN 1 ELSE 0 END) AS shots_window, "
            "       SUM(CASE WHEN captured_at >= ? AND ocr_status = 'failed' "
            "                THEN 1 ELSE 0 END) AS ocr_failed, "
            "       SUM(CASE WHEN captured_at >= ? AND ocr_status = 'done' "
            "                THEN 1 ELSE 0 END) AS ocr_done "
            "FROM screenshots "
            "WHERE app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name",
            (cutoff_iso, cutoff_iso, cutoff_iso),
        )
        async for row in cursor:
            app_name = str(row["app_name"])
            last_seen_raw = row["last_seen"]
            if last_seen_raw is None:
                # MAX(captured_at) is NULL only when every row in the
                # group has a NULL captured_at — schema disallows it,
                # but defend anyway so a corrupt row never crashes the
                # dashboard.
                log.warning(
                    "app_health.null_last_seen_skipped",
                    app_name=app_name,
                )
                continue

            shots_window = int(row["shots_window"] or 0)
            ocr_failed = int(row["ocr_failed"] or 0)
            ocr_done = int(row["ocr_done"] or 0)

            denominator = ocr_failed + ocr_done
            fail_rate = (
                round(ocr_failed / denominator * 100.0, 1)
                if denominator > 0
                else 0.0
            )

            rows.append(
                AppHealthRow(
                    app_name=app_name,
                    last_seen=str(last_seen_raw),
                    shots_7d=shots_window,
                    ocr_fail_rate_pct=fail_rate,
                )
            )

    # Sort: busiest first, then alphabetical for determinism. Stable
    # sort + two passes keeps the ties predictable.
    rows.sort(key=lambda r: r["app_name"])
    rows.sort(key=lambda r: r["shots_7d"], reverse=True)

    log.info(
        "app_health.computed",
        days=window,
        apps=len(rows),
        total_window_shots=sum(r["shots_7d"] for r in rows),
    )
    return rows
