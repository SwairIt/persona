"""Prometheus-compatible metrics exporter for Persona.

Why this exists
---------------
Persona already exposes ``/admin/health`` and ``/api/health.json`` for a
human / browser-based read of "is it alive, is it caught up". That works
for ad-hoc inspection but does not plug into the standard monitoring
stack (Prometheus pull + Grafana dashboards + Alertmanager) that
power-users already run for the rest of their box.

This module renders a Prometheus *text-format 0.0.4* payload that
``/metrics`` serves. We hand-roll the formatter instead of pulling in
``prometheus_client``: the spec is tiny (HELP + TYPE + ``name{labels} value``
per line, newline-terminated), our metric set is small and stable, and
the dependency budget for Persona is deliberately tight.

Metrics surfaced
----------------
Capture-domain counters (``COUNT(*)`` of the canonical artefact tables):

* ``persona_screenshots_total``     — every screenshot ever captured.
* ``persona_audio_segments_total``  — every audio segment landed.
* ``persona_pins_total``            — every ``daily_pin`` row.
* ``persona_hourly_cards_total``    — every ``hourly_card`` row.

Entity ledger (gauge, one series per ``kind``):

* ``persona_entity_count{kind="..."}`` — current per-kind row count.

Storage budget (today only, gauges):

* ``persona_today_bytes_used``  — sum across every bucket in
  ``daily_budget_state`` for today.
* ``persona_budget_cap_bytes``  — ``daily_budget_mb`` from settings,
  expressed in bytes for symmetry with the used-bytes gauge.
* ``persona_throttle_level``    — current 0..3 level as cached by
  :func:`app.budget.get_throttle_level`.

Worker freshness:

* ``persona_workers_heartbeat_age_seconds{worker="..."}`` — wall-clock
  seconds since each worker's last heartbeat. One series per worker;
  empty series-set is valid (= no workers have ever beat on this box).

Design notes
------------
* **Counter naming.** Each cumulative metric carries the ``_total``
  suffix as the spec strongly recommends — Prometheus' ``rate()`` and
  ``increase()`` functions key off this convention.
* **Parametrised SQL.** Every query uses placeholders (none of these
  reads inline external input today, but we still go through the safe
  API so future edits do not regress).
* **Label escaping.** ``kind`` and ``worker`` labels are user-derived
  (``entity.kind`` is constrained by a CHECK constraint, worker names
  come from our own code) but the formatter still escapes ``\\``, ``"``,
  and ``\\n`` per spec so any future free-form label value stays safe.
* **Cheap.** All reads are simple ``COUNT(*)`` / single-row scans, so
  scraping at the default Prometheus 15 s cadence is fine.
* **Best-effort heartbeats.** :func:`app.workers.heartbeat.get_all`
  already swallows DB errors and returns an empty list, so we get the
  same robustness here for free — a transient SQLite hiccup at scrape
  time emits an empty heartbeat series-set rather than 500.
"""

from __future__ import annotations

from typing import Final

from app.budget import get_throttle_level
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.heartbeat import get_all as get_all_heartbeats

log = get_logger("persona.metrics_export")

# Prometheus text-format 0.0.4 separator: every line is LF-terminated and
# the body ends with a final LF. We assemble lines into a list and join
# at the end so the formatter is allocation-light and easy to audit.
_LF: Final[str] = "\n"


async def build_metrics_text() -> str:
    """Render the full Prometheus text-format payload.

    Returns
    -------
    str
        UTF-8 text-format 0.0.4 body. Trailing newline included.
    """
    settings = get_settings()

    screenshots = await _count_rows("screenshots")
    audio_segments = await _count_rows("audio_segment")
    pins = await _count_rows("daily_pin")
    hourly_cards = await _count_rows("hourly_card")
    entity_counts = await _count_entities_by_kind()
    today_bytes = await _sum_today_budget_bytes()
    throttle_level = await get_throttle_level()
    cap_bytes = int(settings.daily_budget_mb * 1024 * 1024)
    heartbeats = await get_all_heartbeats()

    lines: list[str] = []

    _emit_counter(
        lines,
        name="persona_screenshots_total",
        help_text="Total number of screenshots captured (lifetime, COUNT(*) screenshots).",
        value=screenshots,
    )
    _emit_counter(
        lines,
        name="persona_audio_segments_total",
        help_text="Total number of audio segments landed (lifetime, COUNT(*) audio_segment).",
        value=audio_segments,
    )
    _emit_counter(
        lines,
        name="persona_pins_total",
        help_text="Total number of pinned days (lifetime, COUNT(*) daily_pin).",
        value=pins,
    )
    _emit_counter(
        lines,
        name="persona_hourly_cards_total",
        help_text="Total number of hourly summary cards produced (lifetime, COUNT(*) hourly_card).",
        value=hourly_cards,
    )

    _emit_gauge_header(
        lines,
        name="persona_entity_count",
        help_text="Current count of rows in entity, grouped by kind.",
    )
    # Sorted so the output is stable across scrapes — keeps diff-based
    # eyeballing of /metrics sane during dev.
    for kind, count in sorted(entity_counts.items()):
        lines.append(f'persona_entity_count{{kind="{_escape_label(kind)}"}} {count}')

    _emit_gauge(
        lines,
        name="persona_today_bytes_used",
        help_text="Bytes written today across every storage bucket (sum of daily_budget_state).",
        value=today_bytes,
    )
    _emit_gauge(
        lines,
        name="persona_budget_cap_bytes",
        help_text="Configured daily storage cap (daily_budget_mb * 1024 * 1024).",
        value=cap_bytes,
    )
    _emit_gauge(
        lines,
        name="persona_throttle_level",
        help_text="Current throttle level: 0=normal, 1=mild, 2=strict, 3=emergency.",
        value=int(throttle_level),
    )

    _emit_gauge_header(
        lines,
        name="persona_workers_heartbeat_age_seconds",
        help_text="Seconds since each worker's last heartbeat. -1 when unparseable.",
    )
    for row in heartbeats:
        worker_label = _escape_label(row["name"])
        # ``seconds_since`` is a float; render with one decimal place to
        # keep the payload compact while preserving sub-second resolution
        # for fast workers.
        lines.append(
            f'persona_workers_heartbeat_age_seconds{{worker="{worker_label}"}}'
            f" {row['seconds_since']:.3f}"
        )

    log.info(
        "metrics_export.served",
        screenshots=screenshots,
        audio_segments=audio_segments,
        pins=pins,
        hourly_cards=hourly_cards,
        entity_kinds=len(entity_counts),
        today_bytes=today_bytes,
        throttle_level=int(throttle_level),
        worker_count=len(heartbeats),
    )

    # Spec requires a final LF after the last metric line.
    return _LF.join(lines) + _LF


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Whitelist of tables this module is allowed to ``COUNT(*)``. Identifier
# placeholders are not supported by SQLite, so the table name has to be
# spliced into the SQL literally — we gate that splice on this set so
# the call site stays trivially injection-proof regardless of how the
# argument was constructed.
_ALLOWED_COUNT_TABLES: Final[frozenset[str]] = frozenset(
    {"screenshots", "audio_segment", "daily_pin", "hourly_card"}
)


async def _count_rows(table: str) -> int:
    """Return ``COUNT(*)`` for ``table``.

    ``table`` must be in :data:`_ALLOWED_COUNT_TABLES`; anything else is
    a programming error and raises ``ValueError`` so a typo never reaches
    the SQLite layer.
    """
    if table not in _ALLOWED_COUNT_TABLES:
        msg = f"metrics_export: refusing to COUNT(*) on unlisted table {table!r}"
        raise ValueError(msg)
    # Bandit/ruff S608 flags string-built SQL; the whitelist guard above
    # makes the splice safe. The query itself takes no parameters.
    sql = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(sql)
            row = await cursor.fetchone()
    except Exception as exc:
        log.warning("metrics_export.count_failed", table=table, error=str(exc))
        return 0
    if row is None:
        return 0
    return int(row["n"])


async def _count_entities_by_kind() -> dict[str, int]:
    """Return ``{kind: COUNT(*)}`` across the ``entity`` ledger."""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT kind, COUNT(*) AS n FROM entity GROUP BY kind")
            rows = await cursor.fetchall()
    except Exception as exc:
        log.warning("metrics_export.entity_count_failed", error=str(exc))
        return {}
    return {str(row["kind"]): int(row["n"]) for row in rows}


async def _sum_today_budget_bytes() -> int:
    """Return today's bytes-used sum across every bucket. Zero if no row."""
    # ``date('now')`` picks today in UTC inside SQLite; matches the
    # ``_today_utc()`` convention used by :mod:`app.budget`.
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT thumbnails_bytes, audio_bytes, events_bytes, "
                "ocr_text_bytes, embeddings_bytes, misc_bytes "
                "FROM daily_budget_state WHERE day = date('now')"
            )
            row = await cursor.fetchone()
    except Exception as exc:
        log.warning("metrics_export.budget_read_failed", error=str(exc))
        return 0
    if row is None:
        return 0
    return (
        int(row["thumbnails_bytes"])
        + int(row["audio_bytes"])
        + int(row["events_bytes"])
        + int(row["ocr_text_bytes"])
        + int(row["embeddings_bytes"])
        + int(row["misc_bytes"])
    )


def _emit_counter(lines: list[str], *, name: str, help_text: str, value: int) -> None:
    """Append HELP/TYPE/value lines for a counter metric."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    lines.append(f"{name} {int(value)}")


def _emit_gauge(lines: list[str], *, name: str, help_text: str, value: int) -> None:
    """Append HELP/TYPE/value lines for an unlabelled gauge."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    lines.append(f"{name} {int(value)}")


def _emit_gauge_header(lines: list[str], *, name: str, help_text: str) -> None:
    """Append HELP/TYPE lines for a labelled gauge.

    The caller appends one ``name{label="..."} value`` line per series
    after this header. Emitting a header without any series is valid
    per spec — Prometheus will simply record zero matching series.
    """
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")


def _escape_label(value: str) -> str:
    """Escape a label value per Prometheus text-format 0.0.4 rules.

    Replace ``\\`` with ``\\\\``, ``"`` with ``\\"``, and literal newline
    with ``\\n``. Order matters: backslash must be doubled first so the
    other replacements' inserted backslashes are not themselves escaped.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


__all__ = ["build_metrics_text"]
