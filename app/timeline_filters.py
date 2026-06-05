"""Timeline filter-chip catalogue and state builder.

This module ships the *data layer* for the new timeline chip bar — a
small horizontal row of toggleable filter chips that will sit at the top
of ``/`` (the timeline route) and let the user narrow the visible
captures without leaving the page.

Integration plan (next tick — NOT this feature)
-----------------------------------------------
1. ``app/web/routes/timeline.py::home`` will accept a new
   ``chips: str | None`` ``Query`` param (CSV of chip ids — e.g.
   ``chips=pinned_only,has_ocr``).
2. ``home`` will call :func:`build_chip_state` to translate the active
   chip ids into a list of parametrised SQL fragments + bound params.
3. The fragments will be ANDed onto the ``list_screenshots`` query (or a
   thin wrapper around it) so the existing repository helper does not
   need to learn each new flag.
4. The chip catalogue is also exposed via
   ``GET /api/timeline/filters/state.json`` so external dashboards / the
   future mobile UI can mirror the same filter set without duplicating
   the SQL.
5. The HTML chip bar partial (``_timeline_filter_chips.html``) is
   embeddable via HTMX from ``GET /widget/timeline-filter-chips`` so the
   timeline template only needs a single ``hx-get`` line to pull it in.

Until the integration tick lands, calling :func:`build_chip_state`
already returns a usable ``sql_clauses`` payload; the only thing missing
is the ``WHERE`` glue in :mod:`app.web.routes.timeline`. Shipping the
catalogue + helper + JSON state endpoint first keeps the diff small and
lets the chip bar be designed / styled in isolation.

Why a catalogue, not hard-coded chips
-------------------------------------
Every chip is a row in :data:`TIMELINE_FILTERS` — adding a new toggle
becomes a one-record append plus a parametrised SQL clause, and the
JSON state endpoint picks it up automatically. The catalogue keeps the
SQL *next to* the human label so the chip bar and the query layer can
never drift.

SQL safety
----------
All clauses are *parametrised*. ``sql_clause_template`` is a static
``str`` literal (no f-string interpolation of user input), and any
placeholders are bound via the ``params`` list returned by
:func:`build_chip_state`. The ``app`` and ``window`` filters are
intentionally implemented as pure "route to existing ``?app=`` /
``?window=``" markers — they do not emit a SQL fragment, so they can
never inject text into a query.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.logging_setup import get_logger

log = get_logger("persona.timeline_filters")


class TimelineFilter(TypedDict):
    """A single togglable chip in the timeline filter bar.

    Attributes
    ----------
    chip_id:
        Stable machine identifier — what gets serialised into the
        ``?chips=`` URL CSV. Must be a valid Python identifier and
        URL-safe (``[a-z0-9_]``).
    label:
        Short human-readable label rendered inside the chip pill.
    default_active:
        Whether the chip is active when the page is loaded without an
        explicit ``?chips=`` parameter. Always ``False`` today — we ship
        the bar fully neutral so the initial render matches the legacy
        view.
    sql_clause_template:
        Parametrised SQL fragment that gets ANDed into the timeline
        query when the chip is active. ``""`` for chips that route to a
        pre-existing query parameter (``app_filter`` / ``window_filter``)
        rather than emitting their own ``WHERE`` clause.
    description:
        One-line plain-English explanation surfaced on the
        ``/timeline/filters`` help page.
    """

    chip_id: str
    label: str
    default_active: bool
    sql_clause_template: str
    description: str


# Catalogue of every chip the timeline bar can render. The order in this
# list IS the render order in the UI.
#
# When adding a new chip:
#   1. Append a ``TimelineFilter`` dict here.
#   2. If it needs a new ``WHERE`` shape, write the clause as a literal
#      string with ``?`` placeholders — NEVER interpolate the user's
#      input via f-string or ``%`` formatting.
#   3. If the clause needs bound params (e.g. a ``current_date``), wire
#      them up in :func:`_clause_params` below.
TIMELINE_FILTERS: list[TimelineFilter] = [
    {
        "chip_id": "today",
        "label": "Today",
        "default_active": False,
        "sql_clause_template": "DATE(captured_at) = DATE('now', 'localtime')",
        "description": "Show only captures from today (local timezone).",
    },
    {
        "chip_id": "pinned_only",
        "label": "Pinned",
        "default_active": False,
        "sql_clause_template": "pinned_at IS NOT NULL",
        "description": "Show only screenshots the user (or auto-pin engine) pinned.",
    },
    {
        "chip_id": "has_ocr",
        "label": "Has OCR",
        "default_active": False,
        "sql_clause_template": "ocr_text IS NOT NULL AND length(ocr_text) > 20",
        "description": "Show only screenshots whose OCR pass produced meaningful text.",
    },
    {
        "chip_id": "has_voice",
        "label": "Has voice",
        "default_active": False,
        "sql_clause_template": (
            "EXISTS (SELECT 1 FROM audio_segment a "
            "WHERE strftime('%Y-%m-%d %H', a.started_at) "
            "= strftime('%Y-%m-%d %H', screenshots.captured_at))"
        ),
        "description": "Show only screenshots taken in the same hour as a voice segment.",
    },
    {
        "chip_id": "app_filter",
        "label": "This app",
        "default_active": False,
        "sql_clause_template": "",  # routed to existing ?app= param
        "description": "Pin the current ?app= filter as a chip (no extra SQL).",
    },
    {
        "chip_id": "window_filter",
        "label": "This window",
        "default_active": False,
        "sql_clause_template": "",  # routed to existing ?window= param
        "description": "Pin the current ?window= filter as a chip (no extra SQL).",
    },
]


class ChipView(TypedDict):
    """Single-chip view payload returned by :func:`build_chip_state`."""

    chip_id: str
    label: str
    active: bool
    description: str
    routes_to_param: str | None


class ChipStateResult(TypedDict):
    """Aggregate state payload returned by :func:`build_chip_state`."""

    chips: list[ChipView]
    sql_clauses: list[str]
    sql_params: list[Any]
    active_chip_ids: list[str]


def _normalise_active(active_chips: list[str]) -> set[str]:
    """Drop unknown chip ids; collapse to a deduplicated set.

    Anything not in the catalogue is dropped silently — a stray
    ``?chips=evil`` from a stale bookmark must never turn into a server
    error.
    """
    known = {f["chip_id"] for f in TIMELINE_FILTERS}
    return {chip for chip in active_chips if chip in known}


def _routes_to_param(chip_id: str) -> str | None:
    """Return the legacy query-param a chip routes to, if any.

    ``app_filter`` and ``window_filter`` do not emit SQL; instead, the
    timeline route already understands ``?app=`` and ``?window=``. The
    chip bar just visualises those existing filters as toggleable pills.
    """
    if chip_id == "app_filter":
        return "app"
    if chip_id == "window_filter":
        return "window"
    return None


async def build_chip_state(
    active_chips: list[str],
    app: str | None = None,
    window: str | None = None,
) -> ChipStateResult:
    """Translate active chip ids into a render payload + SQL fragments.

    Parameters
    ----------
    active_chips:
        Chip ids the user toggled on, typically from the ``?chips=`` CSV.
    app:
        Current ``?app=`` value, if any. When set, the ``app_filter``
        chip is rendered as active even if not explicitly listed in
        ``active_chips`` — the URL param IS the chip state for those two.
    window:
        Current ``?window=`` value, mirroring ``app``.

    Returns
    -------
    ChipStateResult
        A dict with:

        * ``chips`` — one :class:`ChipView` per catalogue entry, in
          render order, with ``active`` resolved against the inputs.
        * ``sql_clauses`` — list of parametrised SQL fragments to AND
          onto the timeline query.
        * ``sql_params`` — list of bound params in the order the
          fragments expect them. (Currently every fragment is
          self-contained, so this is always ``[]`` — but the field is
          part of the public contract so callers can plumb it through
          without an API break later.)
        * ``active_chip_ids`` — the cleaned, deduplicated chip-id list.
    """
    active = _normalise_active(active_chips)

    # The routed chips ALSO follow the URL param — if ``?app=Slack`` is
    # set, the "this app" chip is visually active even when the caller
    # didn't list ``app_filter`` in ``active_chips``. This is the only
    # source of truth for those two.
    if app:
        active.add("app_filter")
    if window:
        active.add("window_filter")

    chips: list[ChipView] = []
    sql_clauses: list[str] = []
    sql_params: list[Any] = []

    for entry in TIMELINE_FILTERS:
        chip_id = entry["chip_id"]
        is_active = chip_id in active
        chips.append(
            {
                "chip_id": chip_id,
                "label": entry["label"],
                "active": is_active,
                "description": entry["description"],
                "routes_to_param": _routes_to_param(chip_id),
            }
        )

        if not is_active:
            continue

        clause = entry["sql_clause_template"]
        if not clause:
            # Routed-only chips (app_filter / window_filter) carry no SQL.
            continue
        sql_clauses.append(clause)

    log.info(
        "timeline_filters.state",
        active=sorted(active),
        sql_count=len(sql_clauses),
        app=app,
        window=window,
    )

    return {
        "chips": chips,
        "sql_clauses": sql_clauses,
        "sql_params": sql_params,
        "active_chip_ids": sorted(active),
    }
