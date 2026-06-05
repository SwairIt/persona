"""Per-app budget breach poller (v1.45).

Wakes every 5 minutes, evaluates every enabled budget row, and pushes
a notification (severity from the row) the first time today's tally
crosses the cap for a given app. Dedup is via a kv marker keyed by
``last_alerted_for_<app>_<YYYY-MM-DD>`` so:

* a breach that persists past midnight gets a fresh alert the next day
* the same breach stays quiet for the rest of *today* even if the
  worker restarts mid-cycle

The entire alerting subsystem is master-switched by the kv row
``app_budget_check_enabled`` ("1" default, "0" disables). The settings
page surfaces the toggle so the operator can mute everything without
deleting individual budgets.

The poller is modelled on :class:`BackfillRunner` even though there is
no real "missing key" set — the runner happily accepts a single
sentinel from ``list_missing`` per cycle, which keeps the loop, error
trapping and heartbeat in one place rather than duplicating that
machinery here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app import notifications
from app.app_budgets import check_today_status
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

POLL_INTERVAL_SECONDS: int = 300

_MASTER_KV: str = "app_budget_check_enabled"
_ALERT_MARKER_PREFIX: str = "last_alerted_for_"

log = get_logger("persona.workers.app_budget")


async def _is_master_enabled() -> bool:
    """Return whether the alerting subsystem is on.

    Defaults to ``True`` when the kv row is absent so a fresh install
    starts in the same "alerts work out of the box" state the settings
    page advertises.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _MASTER_KV)
    if raw is None:
        return True
    return raw.strip() == "1"


async def _list_sentinel() -> list[str]:
    """Yield exactly one sentinel item per cycle when alerting is on.

    Returning an empty list when the master kv is off lets the
    :class:`BackfillRunner` log a clean "no missing keys" tick without
    us having to special-case anywhere else.
    """
    if not await _is_master_enabled():
        return []
    return ["cycle"]


async def _alert_key(app_name: str) -> str:
    today = datetime.now(tz=UTC).date().isoformat()
    return f"{_ALERT_MARKER_PREFIX}{app_name}_{today}"


async def _process_cycle(_sentinel: Any) -> str | None:
    """Tally every enabled budget; push one notification per fresh breach.

    Returns a short marker string when at least one notification was
    pushed (so the runner's ``built`` counter ticks up); ``None``
    otherwise. Exceptions inside the loop are logged and swallowed so a
    single broken row doesn't muzzle the rest.
    """
    entries = await check_today_status()
    pushed = 0

    for entry in entries:
        breached_at = entry.get("breached_at")
        if not breached_at:
            continue

        app_name = str(entry["app_name"])
        key = await _alert_key(app_name)
        async with get_connection() as conn:
            already = await get_kv(conn, key)
        if already:
            continue

        severity = str(entry.get("alert_severity", "info"))
        if severity not in {"info", "warn"}:
            # The CHECK constraint should prevent this, but a manual
            # poke at the table could slip through. Fall back to info
            # so notifications.push doesn't raise.
            log.warning(
                "app_budget.invalid_severity",
                app_name=app_name,
                severity=severity,
            )
            severity = "info"

        used = float(entry.get("used_minutes", 0.0))
        cap = int(entry.get("cap_minutes", 0))
        try:
            await notifications.push(
                kind="app_budget.breached",
                title=f"{app_name}: daily budget exceeded",
                body=(
                    f"{used:.0f} min used of a {cap} min cap today."
                    " Set or adjust the cap in /settings/app-budgets."
                ),
                link="/settings/app-budgets",
                severity=severity,
            )
        except Exception as exc:
            log.exception(
                "app_budget.push_failed",
                app_name=app_name,
                error=str(exc),
            )
            continue

        async with get_connection() as conn:
            await set_kv(conn, key, breached_at)
        pushed += 1
        log.info(
            "app_budget.alerted",
            app_name=app_name,
            severity=severity,
            used_minutes=used,
            cap_minutes=cap,
        )

    return f"pushed={pushed}" if pushed else None


async def run_app_budget_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="app-budget-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_sentinel,
        build_one=_process_cycle,
    )
    await runner.run(stop_event)


__all__ = ["POLL_INTERVAL_SECONDS", "run_app_budget_worker"]
