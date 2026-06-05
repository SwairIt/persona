"""Extended Prometheus metrics exporter for Persona (v1.42).

Why this exists
---------------
The v1.41 :mod:`app.metrics_export` payload covers the headline
counters / gauges (lifetime artefact counts, entity-by-kind, today's
storage spend, throttle level, worker heartbeat age) that a stock
Grafana dashboard needs. Power-users running deeper SRE-style
dashboards asked for a few extra signals that don't belong in the
default scrape (more rows, slightly more expensive to compute) but are
extremely valuable for capacity / cost-tracking workflows:

* **Per-worker job count.** Each background worker bumps
  ``worker_heartbeat.ticks`` on every loop iteration; that field is
  already the canonical "how many jobs did this worker run since the
  process started" counter — surfacing it as a Prometheus counter lets
  Grafana plot ``rate(persona_worker_job_count[5m])`` to spot a stuck
  worker before the heartbeat age alert fires.
* **Per-worker last-error age.** ``audit_log`` rows with
  ``success = 0`` whose ``actor`` matches a worker name flag the last
  failed run; "seconds since" mirrors the heartbeat-age gauge so a
  single Grafana panel can co-plot freshness and failure recency.
* **LLM token-cost rollup.** :mod:`app.llm_cost` already estimates USD
  spend from the ``llm_usage`` ledger; we sum *all-time* est_cost across
  every row and emit a counter, so ``increase(...[30d])`` answers "how
  much did the BYO LLM cost me this month?".
* **OCR backlog.** ``COUNT(*) FROM screenshots WHERE ocr_status =
  'pending'`` is the textbook saturation signal for the OCR worker.
* **Smart-pin suggestion backlog.** Pending review queue depth from
  ``smart_pin_suggestion`` — same idea, different worker.
* **Today's capture sessions.** Count of ``capture_session`` rows whose
  ``started_at`` falls in today's UTC date; a quick "am I working
  today?" signal that complements the existing storage gauges.

Design rules
------------
* **Parallel endpoint.** This module is invoked by
  :mod:`app.web.routes.metrics_extended` only — the v1.41 ``/metrics``
  endpoint is untouched. That way a misbehaving extended query (slow
  ``audit_log`` scan on a years-old DB, say) can never break the
  standard Prometheus scrape an operator already runs.
* **Reuses the v1.41 base.** We start by calling
  :func:`app.metrics_export.build_metrics_text` and append additional
  HELP/TYPE/value triplets — Grafana sees the same series-set plus the
  extras. Spec also stays text-format 0.0.4 compliant (one final LF).
* **Best-effort reads.** Every helper swallows DB errors and returns a
  neutral fallback (``0`` / ``{}`` / ``[]``). A transient SQLite hiccup
  at scrape time emits zeroes rather than 500.
* **Parametrised SQL.** Every query that takes user-/caller-derived
  values uses placeholders. ``COUNT(*)`` reads with no parameters are
  literal SQL.
* **Label escaping.** Worker names come from our own code today but the
  formatter still escapes ``\\``, ``"`` and ``\\n`` so future free-form
  labels stay safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from app.logging_setup import get_logger
from app.metrics_export import build_metrics_text
from app.storage.db import get_connection
from app.workers.heartbeat import get_all as get_all_heartbeats

log = get_logger("persona.metrics_extended")

_LF: Final[str] = "\n"


async def build_extended_metrics_text() -> str:
    """Render the extended Prometheus text-format payload.

    Returns the v1.41 base payload concatenated with the additional
    series described in the module docstring. Trailing newline included.
    """
    base = await build_metrics_text()

    worker_ticks = await _worker_job_counts()
    worker_error_ages = await _worker_last_error_ages()
    llm_cost_usd_total = await _llm_token_cost_usd_total()
    ocr_queue = await _ocr_queue_depth()
    smart_pin_pending = await _smart_pin_pending_count()
    sessions_today = await _capture_session_today_count()

    lines: list[str] = []

    # --- per-worker job count -------------------------------------------------
    _emit_counter_header(
        lines,
        name="persona_worker_job_count",
        help_text=(
            "Total background-loop iterations recorded per worker "
            "(worker_heartbeat.ticks). Counter — use rate() / increase()."
        ),
    )
    for name, ticks in sorted(worker_ticks.items()):
        worker_label = _escape_label(name)
        lines.append(
            f'persona_worker_job_count{{worker="{worker_label}"}} {int(ticks)}'
        )

    # --- per-worker last-error age -------------------------------------------
    _emit_gauge_header(
        lines,
        name="persona_worker_last_error_age_seconds",
        help_text=(
            "Seconds since each worker's last failed run "
            "(MAX(audit_log.ts) WHERE success=0 AND actor=worker). "
            "Series only emitted for workers with at least one failure on record."
        ),
    )
    for name, age_seconds in sorted(worker_error_ages.items()):
        worker_label = _escape_label(name)
        lines.append(
            f'persona_worker_last_error_age_seconds{{worker="{worker_label}"}}'
            f" {age_seconds:.3f}"
        )

    # --- LLM token cost (USD) -------------------------------------------------
    _emit_counter(
        lines,
        name="persona_llm_token_cost_usd_total",
        help_text=(
            "Estimated lifetime BYO LLM cost in USD, summed from llm_usage "
            "via app.llm_cost price table. Emits 0 when llm_usage is absent."
        ),
        value_float=llm_cost_usd_total,
    )

    # --- OCR queue depth ------------------------------------------------------
    _emit_gauge(
        lines,
        name="persona_ocr_queue_depth",
        help_text=(
            "Screenshots awaiting OCR "
            "(COUNT(*) FROM screenshots WHERE ocr_status = 'pending')."
        ),
        value=ocr_queue,
    )

    # --- Smart-pin pending backlog -------------------------------------------
    _emit_gauge(
        lines,
        name="persona_smart_pin_pending_count",
        help_text=(
            "Pending smart-pin suggestions awaiting user review "
            "(accepted_at IS NULL AND dismissed_at IS NULL)."
        ),
        value=smart_pin_pending,
    )

    # --- Today's capture sessions --------------------------------------------
    _emit_gauge(
        lines,
        name="persona_capture_session_today_count",
        help_text=(
            "Number of capture_session rows whose started_at falls on today "
            "(UTC). Zero on a no-work day is legitimate."
        ),
        value=sessions_today,
    )

    log.info(
        "metrics_extended.served",
        worker_count=len(worker_ticks),
        worker_error_count=len(worker_error_ages),
        llm_cost_usd=round(llm_cost_usd_total, 4),
        ocr_queue_depth=ocr_queue,
        smart_pin_pending=smart_pin_pending,
        sessions_today=sessions_today,
    )

    # Concatenate base (already ends with LF) + the extended block + final LF.
    extra = _LF.join(lines)
    if not extra:
        return base
    return base + extra + _LF


# ---------------------------------------------------------------------------
# Internal helpers — DB reads
# ---------------------------------------------------------------------------


async def _worker_job_counts() -> dict[str, int]:
    """Return ``{worker_name: ticks}`` from ``worker_heartbeat``.

    ``ticks`` is the canonical per-worker job counter — incremented by
    :func:`app.workers.heartbeat.beat` on every loop iteration. Reading
    it via :func:`get_all_heartbeats` keeps us in sync with the rest of
    the health surface (``/admin/health``, ``/api/health.json``,
    ``persona_workers_heartbeat_age_seconds`` in v1.41).
    """
    try:
        rows = await get_all_heartbeats()
    except Exception as exc:  # noqa: BLE001 — defensive: scrape must not 500
        log.warning("metrics_extended.heartbeat_read_failed", error=str(exc))
        return {}
    return {str(row["name"]): int(row["ticks"]) for row in rows}


async def _worker_last_error_ages() -> dict[str, float]:
    """Return ``{worker_name: seconds_since_last_failure}``.

    Aggregates ``audit_log`` rows with ``success = 0`` grouped by the
    ``actor`` column (which workers populate with their own name when
    they record a failure). Workers with no failure on record are
    omitted — a missing series in Prometheus is the right "no data"
    signal here, distinct from "the failure was 0 s ago".
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT actor, MAX(ts) AS last_ts FROM audit_log "
                "WHERE success = 0 AND actor IS NOT NULL "
                "GROUP BY actor"
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics_extended.audit_read_failed", error=str(exc))
        return {}

    now = datetime.now(UTC)
    out: dict[str, float] = {}
    for row in rows:
        actor = row["actor"]
        last_ts_raw = row["last_ts"]
        if actor is None or last_ts_raw is None:
            continue
        try:
            # ``audit_log.ts`` is SQLite ``datetime('now')`` — naive UTC,
            # format ``YYYY-MM-DD HH:MM:SS``. Stamp it as UTC so the
            # subtraction below stays tz-aware.
            last_ts = datetime.strptime(
                str(last_ts_raw), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=UTC)
        except ValueError:
            # Unparseable timestamp — skip the series rather than emit
            # a nonsense value. A future migration to ISO-8601 with
            # fractional seconds would need a second branch here.
            log.warning(
                "metrics_extended.audit_ts_unparseable",
                actor=str(actor),
                ts=str(last_ts_raw),
            )
            continue
        delta = (now - last_ts).total_seconds()
        # Clamp tiny negative drift (clock skew between INSERT and read)
        # to zero — a negative age would be meaningless in Grafana.
        out[str(actor)] = max(0.0, delta)
    return out


async def _llm_token_cost_usd_total() -> float:
    """Return the lifetime estimated USD spend across ``llm_usage``.

    Mirrors the pricing logic in :mod:`app.llm_cost` — we keep the
    price table in one place and re-use it here so a freshly-supported
    model only needs an entry there. If the ``llm_usage`` table is
    missing (operator never enabled the BYO LLM path) we return ``0``
    without escalating the error.
    """
    try:
        # Lazy import keeps the module importable on installs that haven't
        # yet had migration 079 applied (test fixtures, fresh checkouts).
        from app.llm_cost import (  # noqa: PLC0415 — see docstring
            _DEFAULT_MODEL_BY_PROVIDER,
            _estimate_cost_usd,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics_extended.llm_cost_import_failed", error=str(exc))
        return 0.0

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(provider, 'unknown') AS provider, "
                "       COALESCE(SUM(input_tokens), 0) AS input_total, "
                "       COALESCE(SUM(output_tokens), 0) AS output_total "
                "FROM llm_usage "
                "GROUP BY COALESCE(provider, 'unknown')"
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics_extended.llm_usage_read_failed", error=str(exc))
        return 0.0

    total = 0.0
    for row in rows:
        provider = str(row["provider"]) if row["provider"] else "unknown"
        model = _DEFAULT_MODEL_BY_PROVIDER.get(provider, "unknown")
        input_total = int(row["input_total"] or 0)
        output_total = int(row["output_total"] or 0)
        total += _estimate_cost_usd(provider, model, input_total, output_total)
    return total


async def _ocr_queue_depth() -> int:
    """Return ``COUNT(*) FROM screenshots WHERE ocr_status = 'pending'``."""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM screenshots WHERE ocr_status = ?",
                ("pending",),
            )
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics_extended.ocr_queue_read_failed", error=str(exc))
        return 0
    return int(row["n"]) if row is not None else 0


async def _smart_pin_pending_count() -> int:
    """Return pending smart-pin suggestion count.

    A pending row has both ``accepted_at`` and ``dismissed_at`` NULL —
    same definition the dashboard uses (and the partial index
    ``idx_smart_pin_pending`` covers).
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM smart_pin_suggestion "
                "WHERE accepted_at IS NULL AND dismissed_at IS NULL"
            )
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics_extended.smart_pin_read_failed", error=str(exc))
        return 0
    return int(row["n"]) if row is not None else 0


async def _capture_session_today_count() -> int:
    """Return today's ``capture_session`` count (UTC ``date('now')``).

    Matches the v1.41 ``persona_today_bytes_used`` convention — SQLite's
    ``date('now')`` is UTC, so a single ``WHERE DATE(started_at) = ...``
    filter agrees with the rest of the metrics block.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM capture_session "
                "WHERE DATE(started_at) = date('now')"
            )
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "metrics_extended.capture_session_read_failed", error=str(exc)
        )
        return 0
    return int(row["n"]) if row is not None else 0


# ---------------------------------------------------------------------------
# Internal helpers — text-format 0.0.4 emission
# ---------------------------------------------------------------------------


def _emit_counter(
    lines: list[str],
    *,
    name: str,
    help_text: str,
    value_float: float,
) -> None:
    """Append HELP/TYPE/value lines for an unlabelled float counter.

    Counters in the Prometheus spec are unsigned and monotonic; we still
    render as a float so the LLM-cost case (sub-cent precision) prints
    without rounding to integer dollars.
    """
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    # ``g`` formatter keeps short ints short (no ``.0`` tail) while still
    # carrying meaningful precision for fractional dollars.
    lines.append(f"{name} {value_float:.6f}")


def _emit_counter_header(
    lines: list[str], *, name: str, help_text: str
) -> None:
    """Append HELP/TYPE lines for a labelled counter.

    Mirrors :func:`app.metrics_export._emit_gauge_header` — emitting a
    header with no series is valid spec-wise.
    """
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")


def _emit_gauge(
    lines: list[str], *, name: str, help_text: str, value: int
) -> None:
    """Append HELP/TYPE/value lines for an unlabelled gauge."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    lines.append(f"{name} {int(value)}")


def _emit_gauge_header(
    lines: list[str], *, name: str, help_text: str
) -> None:
    """Append HELP/TYPE lines for a labelled gauge."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")


def _escape_label(value: str) -> str:
    """Escape a label value per Prometheus text-format 0.0.4 rules.

    Same logic as :func:`app.metrics_export._escape_label`; duplicated
    rather than imported because the helper is private to that module
    and we want this file to stand on its own dependency surface.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


__all__ = ["build_extended_metrics_text"]
