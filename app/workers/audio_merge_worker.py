"""Audio segment merge worker (v1.40).

Periodic driver around :mod:`app.audio_segment_merge`. Wraps the merge
primitives in a :class:`app.workers._bases.BackfillRunner` so the
lifespan task layout stays uniform with the other backfill-style
workers (long-read, entity extractor, …).

* ``list_missing`` returns the list of candidate groups from
  :func:`app.audio_segment_merge.find_merge_candidates`. Each group
  becomes one ``build_one`` invocation, so a slow merge of one group
  doesn't starve the rest of the cycle.
* ``build_one`` calls :func:`app.audio_segment_merge.merge_group` and
  returns the result dict; the runner counts truthy returns toward
  ``worker.cycle``.
* A kv kill-switch (``audio_merge_enabled``) gates the whole
  operation. Default is ``1`` (enabled) — fragmented transcripts are
  noise nobody wants, so the feature is opt-out. Setting the kv to
  ``"0"`` returns an empty candidate list and the runner just sleeps.

Cadence is 1800 s (30 min): the producer (audio capture / transcribe)
takes minutes to populate fresh rows and the merge is cheap, so
half-hourly is plenty to keep the canonical view current without
spamming the log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from app.audio_segment_merge import (
    MergeResult,
    find_merge_candidates,
    merge_group,
)
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.audio_merge")


POLL_INTERVAL_SECONDS: Final[int] = 1800
"""30 minutes — see module docstring for rationale."""

_ENABLED_KV: Final[str] = "audio_merge_enabled"
"""kv_settings flag (``0``/``1``) used as a runtime kill-switch."""

_WORKER_NAME: Final[str] = "audio-merge-worker"

_DEFAULT_GAP_SECONDS: Final[float] = 1.0
_DEFAULT_LOOKBACK_HOURS: Final[int] = 24


async def _is_enabled() -> bool:
    """Return ``True`` unless ``audio_merge_enabled`` is literally ``0``.

    Default-on: the kv row is created lazily, so absence == enabled.
    """
    async with get_connection() as conn:
        value = await get_kv(conn, _ENABLED_KV)
    if value is None:
        return True
    return value.strip() != "0"


async def _list_missing() -> list[list[int]]:
    """Return candidate groups while the feature is enabled, else ``[]``."""
    enabled = await _is_enabled()
    if not enabled:
        return []
    groups = await find_merge_candidates(
        gap_seconds=_DEFAULT_GAP_SECONDS,
        lookback_hours=_DEFAULT_LOOKBACK_HOURS,
    )
    return groups


async def _build_one(segment_ids: list[int]) -> MergeResult | None:
    """Merge one candidate group and return the result dict.

    Returns ``None`` only on a degenerate group (<2 ids), in which
    case :class:`BackfillRunner` will not bump its ``built`` counter.
    Any other failure bubbles up and is logged by the runner's
    per-key error handler.
    """
    if len(segment_ids) < 2:
        return None
    result = await merge_group(segment_ids)
    log.info(
        "audio_merge_worker.merged",
        merged_into_id=result["merged_into_id"],
        count_merged=result["count_merged"],
        total_duration_seconds=result["total_duration_seconds"],
    )
    return result


async def run_audio_merge_worker(
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
    "run_audio_merge_worker",
]
