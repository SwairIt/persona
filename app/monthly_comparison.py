"""Monthly comparison report — this month vs the prior calendar month.

A single ``/stats/monthly-comparison`` answer to "how did I do this month
compared to last month?". We pull four headline totals (screenshots,
voice seconds, unique apps, notes created) for both months, compute the
percentage delta of each, and then rank apps by absolute growth /
decline so the operator can see *which* tools drove the change rather
than just the aggregate.

Why a separate module
---------------------

Aggregations live in :mod:`app.today_vs_average`, :mod:`app.audio_stats`
and :mod:`app.personal_metrics` already, but each one is keyed by *day*
or *lifetime*. A month-vs-month rollup needs a slightly different SQL
shape — ``strftime('%Y-%m', captured_at) = ?`` rather than
``DATE(captured_at) = ?`` — and the per-app delta join would noise up
those existing modules. Keeping it standalone matches how
:mod:`app.today_vs_average` carves out its own surface for its widget.

Noise floor
-----------

Per-app deltas are filtered to apps with ``>= 10`` shots in *either*
month before ranking. Without that threshold the "top growth" list
floods with apps that went from 0 → 2 (an "infinite %" jump on a tiny
absolute base) and the report stops being scannable. Ten was picked to
match the ``_TOP_K`` threshold in :mod:`app.tag_trends` so the two
trend surfaces feel coherent.

Output shape
------------

A plain ``dict`` — both the HTML page and the JSON endpoint consume
the same payload, so the dict *is* the public surface. Wrapping it in
a Pydantic model would force every consumer to redo the unpacking.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.monthly_comparison")

# Minimum shot count an app must hit in either month to qualify for the
# growth / decline ranking. Below this we treat the signal as noise.
# Mirrors the threshold used by :mod:`app.tag_trends` for the same
# "tiny base, huge percent" pathology.
_NOISE_FLOOR_SHOTS = 10

# How many apps to surface in each ranking. Five is enough to fit the
# template's per-app comparison bars without overflowing the column.
_TOP_N = 5


class MonthTotals(TypedDict):
    """Aggregate totals for one calendar month."""

    month: str  # YYYY-MM
    days_in_month: int
    shots: int
    voice_seconds: float
    unique_apps: int
    notes_created: int


class Deltas(TypedDict):
    """Absolute and percentage deltas between this_month and last_month."""

    shots: float
    voice: float
    apps: float
    notes: float


class AppDelta(TypedDict):
    """One row of the per-app growth / decline ranking."""

    app: str
    this: int
    last: int
    percent: float


class MonthlyComparison(TypedDict):
    """Full payload returned by :func:`compute_comparison`."""

    this_month: MonthTotals
    last_month: MonthTotals
    deltas: Deltas
    top_growth: list[AppDelta]
    top_declines: list[AppDelta]
    daily_average_this: float
    daily_average_last: float


def _today_local() -> date:
    """Local-date "today" — matches the wall clock the operator sees.

    Mirrors :func:`app.today_vs_average._today_local` so the default
    month inferred from "now" agrees with every other day-keyed page
    rather than drifting by one around midnight in non-UTC timezones.
    """
    return datetime.now().astimezone().date()


def _parse_month_iso(month_iso: str) -> tuple[int, int]:
    """Parse ``YYYY-MM`` into ``(year, month)`` or raise :class:`ValueError`.

    Matches the validator in :mod:`app.monthly_digest_card` and
    :mod:`app.web.routes.monthly_digests` so all the month-keyed pages
    accept and reject the same set of inputs.
    """
    parts = month_iso.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"Bad month format: {month_iso!r}")
    if not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError(f"Bad month digits: {month_iso!r}")
    year = int(parts[0])
    month = int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"Month out of range: {month}")
    return year, month


def _prior_month(year: int, month: int) -> tuple[int, int]:
    """Return the calendar month immediately preceding ``(year, month)``.

    Pure integer arithmetic — no ``dateutil.relativedelta`` dependency
    just for this one trivial subtraction. December rolls back to the
    prior year as expected.
    """
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _month_iso(year: int, month: int) -> str:
    """Format ``(year, month)`` back into the canonical ``YYYY-MM`` string."""
    return f"{year:04d}-{month:02d}"


def _delta_pct(this_value: float, last_value: float) -> float:
    """Percentage delta of ``this_value`` versus ``last_value``.

    Special-cases the zero baseline: ``+100.0 %`` when this month has
    activity and last month had none, ``0.0`` when both are zero, and
    ``-100.0 %`` when this month is zero but last month had activity.
    Rounded to one decimal so the template doesn't have to.

    Mirrors :func:`app.today_vs_average._delta_pct` — the two surfaces
    use the same sign convention so the operator never has to reverse
    a sign mid-page.
    """
    if last_value <= 0.0:
        if this_value > 0.0:
            return 100.0
        return 0.0
    return round(((this_value - last_value) / last_value) * 100.0, 1)


async def _load_month_totals(conn: object, month_iso: str, days_in_month: int) -> MonthTotals:
    """Pull the four headline totals for a single calendar month.

    Every query is parametrised — no string interpolation — and bounded
    by ``strftime('%Y-%m', captured_at) = ?`` for the screenshot- and
    audio-tied counts and by ``strftime('%Y-%m', created_at) = ?`` for
    the standalone ``notes`` table (which has no ``captured_at``).
    """
    # ``conn`` is an :class:`aiosqlite.Connection`; we accept ``object``
    # here so the caller can pass the open connection through without
    # importing the aiosqlite type into this module's public surface.
    cursor = await conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS n FROM screenshots "
        "WHERE strftime('%Y-%m', captured_at) = ?",
        (month_iso,),
    )
    row = await cursor.fetchone()
    shots = int(row["n"]) if row is not None else 0

    cursor = await conn.execute(  # type: ignore[attr-defined]
        "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total "
        "FROM audio_segment "
        "WHERE strftime('%Y-%m', captured_at) = ?",
        (month_iso,),
    )
    row = await cursor.fetchone()
    voice_seconds = float(row["total"]) if row is not None else 0.0

    cursor = await conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(DISTINCT app_name) AS n FROM screenshots "
        "WHERE strftime('%Y-%m', captured_at) = ? "
        "  AND app_name IS NOT NULL AND app_name != ''",
        (month_iso,),
    )
    row = await cursor.fetchone()
    unique_apps = int(row["n"]) if row is not None else 0

    cursor = await conn.execute(  # type: ignore[attr-defined]
        "SELECT COUNT(*) AS n FROM notes "
        "WHERE strftime('%Y-%m', created_at) = ?",
        (month_iso,),
    )
    row = await cursor.fetchone()
    notes_created = int(row["n"]) if row is not None else 0

    return MonthTotals(
        month=month_iso,
        days_in_month=days_in_month,
        shots=shots,
        voice_seconds=voice_seconds,
        unique_apps=unique_apps,
        notes_created=notes_created,
    )


async def _load_per_app(conn: object, month_iso: str) -> dict[str, int]:
    """Return ``{app_name: shot_count}`` for one calendar month.

    Empty / NULL app names are filtered out at the SQL layer so we do
    not have to defend against a sentinel ``""`` key in the caller.
    """
    cursor = await conn.execute(  # type: ignore[attr-defined]
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE strftime('%Y-%m', captured_at) = ? "
        "  AND app_name IS NOT NULL AND app_name != '' "
        "GROUP BY app_name",
        (month_iso,),
    )
    rows = await cursor.fetchall()
    return {str(row["app_name"]): int(row["n"]) for row in rows}


def _rank_app_deltas(
    this_counts: dict[str, int],
    last_counts: dict[str, int],
) -> tuple[list[AppDelta], list[AppDelta]]:
    """Rank apps by absolute shot delta and split into growth / decline.

    The noise floor (``_NOISE_FLOOR_SHOTS``) filters out apps that
    barely registered in either month. Sorting is by ``this - last``
    (absolute, not percentage) because percentage-based ranking has
    been confusing in practice: an app going from 12 → 30 reads more
    consequential than one going from 10 → 80 if you only look at the
    percent, but the latter is what actually shifted the operator's
    month.
    """
    all_apps = set(this_counts) | set(last_counts)
    rows: list[AppDelta] = []
    for app in all_apps:
        this = this_counts.get(app, 0)
        last = last_counts.get(app, 0)
        if max(this, last) < _NOISE_FLOOR_SHOTS:
            continue
        rows.append(
            AppDelta(
                app=app,
                this=this,
                last=last,
                percent=_delta_pct(float(this), float(last)),
            )
        )

    # Sort growth descending (largest absolute increase first) and
    # decline ascending (largest absolute decrease, i.e. most negative,
    # first). A tie-break by ``app`` keeps the ordering deterministic
    # across runs.
    growth = sorted(rows, key=lambda r: (-(r["this"] - r["last"]), r["app"]))
    decline = sorted(rows, key=lambda r: ((r["this"] - r["last"]), r["app"]))
    top_growth = [r for r in growth if r["this"] - r["last"] > 0][:_TOP_N]
    top_declines = [r for r in decline if r["this"] - r["last"] < 0][:_TOP_N]
    return top_growth, top_declines


def _daily_average(total_shots: int, days_in_month: int) -> float:
    """Per-day average shots, rounded to one decimal.

    ``days_in_month`` is always ``>= 28`` for a real calendar month, so
    the divisor is never zero — but we guard anyway because malformed
    upstream input could yield 0 and we'd rather return 0.0 than 500.
    """
    if days_in_month <= 0:
        return 0.0
    return round(total_shots / days_in_month, 1)


async def compute_comparison(month_iso: str | None = None) -> MonthlyComparison:
    """Return totals + deltas + per-app rankings for one month vs the prior.

    Args:
        month_iso: ``YYYY-MM`` for the "this month" side of the
            comparison. Defaults to the current local month so the
            page Just Works on first load.

    Returns:
        :class:`MonthlyComparison`. The shape never changes — empty
        months produce zeros across the board, not ``None`` — so the
        template can rely on the keys.
    """
    if month_iso is None:
        today = _today_local()
        year, month = today.year, today.month
    else:
        year, month = _parse_month_iso(month_iso)

    this_iso = _month_iso(year, month)
    prior_year, prior_month = _prior_month(year, month)
    last_iso = _month_iso(prior_year, prior_month)

    this_days = calendar.monthrange(year, month)[1]
    last_days = calendar.monthrange(prior_year, prior_month)[1]

    async with get_connection() as conn:
        this_totals = await _load_month_totals(conn, this_iso, this_days)
        last_totals = await _load_month_totals(conn, last_iso, last_days)
        this_apps = await _load_per_app(conn, this_iso)
        last_apps = await _load_per_app(conn, last_iso)

    top_growth, top_declines = _rank_app_deltas(this_apps, last_apps)

    deltas = Deltas(
        shots=_delta_pct(float(this_totals["shots"]), float(last_totals["shots"])),
        voice=_delta_pct(this_totals["voice_seconds"], last_totals["voice_seconds"]),
        apps=_delta_pct(
            float(this_totals["unique_apps"]), float(last_totals["unique_apps"])
        ),
        notes=_delta_pct(
            float(this_totals["notes_created"]), float(last_totals["notes_created"])
        ),
    )

    daily_avg_this = _daily_average(this_totals["shots"], this_days)
    daily_avg_last = _daily_average(last_totals["shots"], last_days)

    payload: MonthlyComparison = {
        "this_month": this_totals,
        "last_month": last_totals,
        "deltas": deltas,
        "top_growth": top_growth,
        "top_declines": top_declines,
        "daily_average_this": daily_avg_this,
        "daily_average_last": daily_avg_last,
    }

    log.info(
        "monthly_comparison.computed",
        this_month=this_iso,
        last_month=last_iso,
        this_shots=this_totals["shots"],
        last_shots=last_totals["shots"],
        this_voice=this_totals["voice_seconds"],
        last_voice=last_totals["voice_seconds"],
        this_apps=this_totals["unique_apps"],
        last_apps=last_totals["unique_apps"],
        this_notes=this_totals["notes_created"],
        last_notes=last_totals["notes_created"],
        growth_rows=len(top_growth),
        decline_rows=len(top_declines),
    )

    return payload


__all__ = [
    "AppDelta",
    "Deltas",
    "MonthTotals",
    "MonthlyComparison",
    "compute_comparison",
]
