"""Capture-session detection worker (v1.42).

Periodic driver around :func:`app.capture_sessions.detect_sessions`,
wrapped in :class:`app.workers._bases.BackfillRunner` so the lifespan
task layout stays uniform with the other backfill-style workers
(audio-merge, alt-text, long-read…).

* The detector is whole-table-shaped, not per-row: there isn't a
  natural "missing key" the runner can iterate over. We model the
  whole pass as a single sentinel key. ``list_missing`` returns
  ``["detect"]`` when the feature is enabled, ``[]`` when it isn't —
  so :class:`BackfillRunner` either fires ``build_one`` once per tick
  or sleeps quietly.
* ``build_one`` ignores the sentinel and just calls
  :func:`detect_sessions` with the default lookback / gap window. The
  returned :class:`~app.capture_sessions.DetectStats` is also returned
  from the callback so a truthy result bumps the runner's ``built``
  counter for ``worker.cycle`` log lines.
* A kv kill-switch (``capture_session_detection_enabled``, default 1)
  gates the whole pass. Flipping it to ``"0"`` at runtime makes the
  next tick idle until it's flipped back without restarting the
  daemon.

Cadence is 1800 s (30 min), matching the gap threshold the detector
uses: a session that just closed gets picked up on the very next tick,
keeping the ``/sessions`` view at most ~30 min stale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from app.capture_sessions import DetectStats, detect_sessions
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.capture_session")


POLL_INTERVAL_SECONDS: Final[int] = 1800
"""30 minutes — see module docstring for rationale."""

_ENABLED_KV: Final[str] = "capture_session_detection_enabled"
"""kv_settings flag (``0``/``1``) used as a runtime kill-switch."""

_WORKER_NAME: Final[str] = "capture-session-worker"

_SENTINEL: Final[str] = "detect"
"""Single dummy "missing key" — the detector is whole-table shaped."""


async def _is_enabled() -> bool:
    """Return ``True`` unless ``capture_session_detection_enabled`` is ``0``.

    Default-on: the kv row is created lazily, so absence == enabled.
    """
    async with get_connection() as conn:
        value = await get_kv(conn, _ENABLED_KV)
    if value is None:
        return True
    return value.strip() != "0"


async def _list_missing() -> list[str]:
    """Return a single sentinel when enabled, an empty list otherwise."""
    enabled = await _is_enabled()
    if not enabled:
        return []
    return [_SENTINEL]


async def _build_one(_key: str) -> DetectStats | None:
    """Run one detection pass and return its stats.

    The sentinel argument is intentionally unused — we always run the
    full default-window pass. Returning the stats dict (rather than
    ``None``) ensures :class:`BackfillRunner` bumps its ``built``
    counter and logs ``worker.cycle`` when something actually changed.
    """
    stats = await detect_sessions()
    if stats["inserted"] == 0 and stats["detected"] == 0:
        return None
    log.info(
        "capture_session_worker.cycle",
        detected=stats["detected"],
        inserted=stats["inserted"],
        skipped_duplicates=stats["skipped_duplicates"],
    )
    return stats


async def run_capture_session_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_capture_session_worker",
]
