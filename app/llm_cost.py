"""Per-day LLM cost ledger — token + dollar rollup over the ``llm_usage`` table.

Background
----------
The ``llm_usage`` ledger (migration 079) records one row per BYO LLM call
with ``ts``, ``kind``, ``provider``, ``input_tokens``, ``output_tokens``,
``success``. ``/stats/llm-usage`` already renders the token chart; this
module is the *money* counterpart — it joins the same rows against a
hardcoded price table and surfaces an estimated dollar spend so the
operator can sanity-check their provider bill without juggling tabs.

Schema reality vs. the original spec
------------------------------------
The original feature request asked us to ``GROUP BY day, provider, model,
kind`` and select ``SUM(input_tokens), SUM(output_tokens), COUNT(*)``
filtered by ``created_at``. The actual v0.98 schema has:

* timestamp column = ``ts`` (NOT ``created_at``)
* no ``model`` column at all — the client wrapper persists tokens but not
  the model string

To honour the spec's *intent* (a per-model cost breakdown) without faking
data we don't have, we:

* read the timestamp from the real column ``ts``
* group by ``DATE(ts), provider, kind`` in SQL
* synthesise a ``model`` field per row using ``_DEFAULT_MODEL_BY_PROVIDER``
  which mirrors the constructor defaults in :mod:`app.llm.client`
  (``claude-haiku-4-5`` for anthropic, ``gpt-4o-mini`` for openai,
  ``llama-3.3-70b`` for groq, ``gemini-2.0-flash`` for gemini)

If a future migration adds a ``model`` column we can drop the synthesis
step without touching the route or the template — the dict keys stay
identical.

Pricing
-------
:data:`_PRICE_TABLE` maps ``(provider, model)`` to a USD-per-1M-tokens
tuple ``(input, output)``. Unknown rows fall back to ``(0.0, 0.0)`` and
log a single ``warn`` per unique pair so the operator can spot a
freshly-supported model that needs a price entry — without spamming the
log on every call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.llm_cost")


class DailyCostRow(TypedDict):
    """One row in the per-day breakdown returned by :func:`compute_daily_llm_cost`."""

    day: str
    provider: str
    model: str
    kind: str
    input_total: int
    output_total: int
    calls: int
    est_cost_usd: float


#: USD per 1M tokens, ``(input, output)``. Snapshot taken 2026-06; this is
#: an estimate that won't match a provider bill to the cent — provider
#: pricing pages change without notice and discounts (cache hits, batch,
#: enterprise) aren't modelled. Treat the numbers as ballpark only.
_PRICE_TABLE: Final[dict[tuple[str, str], tuple[float, float]]] = {
    ("anthropic", "claude-haiku-4-5"): (1.0, 5.0),
    ("anthropic", "claude-sonnet-4-5"): (3.0, 15.0),
    ("anthropic", "claude-opus-4-7"): (15.0, 75.0),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("groq", "llama-3.3-70b"): (0.59, 0.79),
    ("gemini", "gemini-2.0-flash"): (0.0, 0.0),
    ("gemini", "gemini-2.0-pro"): (1.25, 5.00),
}

#: Default model assumed for a given ``provider`` when the ledger does not
#: store a model string. Mirrors the constructor defaults in
#: :mod:`app.llm.client` so the cost estimate lines up with what the
#: client actually called by default.
_DEFAULT_MODEL_BY_PROVIDER: Final[dict[str, str]] = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b",
    "gemini": "gemini-2.0-flash",
}

#: De-dup set for "unknown price" warnings — we want to nudge the
#: operator that a price entry is missing, but only once per process,
#: not on every page hit.
_warned_unknown: set[tuple[str, str]] = set()


def _estimate_cost_usd(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Return USD cost for ``(input_tokens, output_tokens)`` at the given model.

    Unknown ``(provider, model)`` pairs return ``0.0`` and emit a single
    ``warn`` log line per unique pair (see :data:`_warned_unknown`). This
    keeps the table renderable on a brand-new provider rather than 500-ing,
    while still surfacing the gap in the operational log.
    """
    key = (provider, model)
    prices = _PRICE_TABLE.get(key)
    if prices is None:
        if key not in _warned_unknown:
            _warned_unknown.add(key)
            log.warning(
                "llm_cost.price_table.miss",
                provider=provider,
                model=model,
                hint="add an entry to app.llm_cost._PRICE_TABLE",
            )
        return 0.0
    in_per_m, out_per_m = prices
    # Pricing is per-million tokens, so divide before multiplying to keep
    # the float in a sensible range even for very large monthly totals.
    return (input_tokens / 1_000_000.0) * in_per_m + (
        output_tokens / 1_000_000.0
    ) * out_per_m


def _window_cutoff_ts(days: int) -> str:
    """ISO-8601 ``YYYY-MM-DD HH:MM:SS`` cutoff for ``ts >= ?`` queries.

    Mirrors :mod:`app.web.routes.llm_usage` — SQLite's ``datetime('now')``
    is wall-clock UTC, so we compute the same way in Python and let the
    indexed ``ts`` column do a pure string compare.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


async def compute_daily_llm_cost(days: int = 30) -> list[DailyCostRow]:
    """Per-day LLM cost rollup over the last ``days`` calendar days.

    Returns a list of :class:`DailyCostRow` dicts grouped by
    ``(day, provider, model, kind)`` — ``model`` is synthesised from
    :data:`_DEFAULT_MODEL_BY_PROVIDER` because the v0.98 ``llm_usage``
    schema does not store the model string per call.

    Parameters
    ----------
    days:
        Number of days back from now to include. Negative / zero values
        are clamped to ``1`` so the SQL window stays well-defined.

    Notes
    -----
    * Parametrised SQL — the cutoff is bound, not interpolated.
    * Failed calls (``success = 0``) are still summed: the provider
      may have billed for a partial generation, and including them
      keeps the ledger total honest against the operator's bill.
    * Rows with ``provider IS NULL`` collapse to the slug ``"unknown"``
      so the per-provider price lookup has a stable key.
    """
    window = max(1, days)
    cutoff_ts = _window_cutoff_ts(window)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(ts) AS day, "
            "       COALESCE(provider, 'unknown') AS provider, "
            "       kind AS kind, "
            "       COALESCE(SUM(input_tokens), 0) AS input_total, "
            "       COALESCE(SUM(output_tokens), 0) AS output_total, "
            "       COUNT(*) AS calls "
            "FROM llm_usage "
            "WHERE ts >= ? "
            "GROUP BY DATE(ts), COALESCE(provider, 'unknown'), kind "
            "ORDER BY day DESC, provider, kind",
            (cutoff_ts,),
        )
        raw_rows = await cursor.fetchall()

    out: list[DailyCostRow] = []
    for row in raw_rows:
        provider = str(row["provider"]) if row["provider"] else "unknown"
        model = _DEFAULT_MODEL_BY_PROVIDER.get(provider, "unknown")
        input_total = int(row["input_total"]) if row["input_total"] else 0
        output_total = int(row["output_total"]) if row["output_total"] else 0
        est = _estimate_cost_usd(provider, model, input_total, output_total)
        out.append(
            {
                "day": str(row["day"]),
                "provider": provider,
                "model": model,
                "kind": str(row["kind"]),
                "input_total": input_total,
                "output_total": output_total,
                "calls": int(row["calls"]),
                "est_cost_usd": round(est, 6),
            }
        )

    log.info(
        "llm_cost.computed",
        days=window,
        rows=len(out),
        total_usd=round(sum(r["est_cost_usd"] for r in out), 4),
    )
    return out


__all__ = ["DailyCostRow", "compute_daily_llm_cost"]
