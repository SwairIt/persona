"""Background pre-renderer for ``audio_segment.waveform_svg``.

Periodic driver around :func:`app.audio_waveform.generate_waveform`.
Wraps the renderer in a :class:`app.workers._bases.BackfillRunner` so
the lifespan task layout stays uniform with the other backfill-style
workers (audio merge, long-read, entity extractor, …).

* ``list_missing`` returns the last 50 ``audio_segment`` rows whose
  ``waveform_svg`` column is still NULL. Bounded to 50 so a fresh
  install with thousands of pre-existing rows doesn't pin the loop
  for an hour on the first tick — the next tick picks up the next
  batch 600 s later.
* ``build_one`` calls :func:`app.audio_waveform.generate_waveform`
  with the default bar count / height. The runner counts every
  non-None return toward ``worker.cycle`` — error / missing returns
  also count (the row was *handled*), they just leave the column NULL
  for the operator to investigate.
* A kv kill-switch (``audio_waveform_enabled``) gates the whole
  operation. Default is ``1`` (enabled) — pre-rendering is cheap and
  the user-visible benefit (snappy timeline pages) is significant.
  Setting the kv to ``"0"`` returns an empty candidate list and the
  runner just sleeps.

Cadence is 600 s (10 minutes): the producer (audio capture +
transcribe) writes new rows continuously and the SVG render is
sub-second per row, so a quarter-hour-ish cadence keeps the backlog
under one screenful without spamming the log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from app.audio_waveform import WaveformResult, generate_waveform
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.audio_waveform")


POLL_INTERVAL_SECONDS: Final[int] = 600
"""10 minutes — see module docstring for rationale."""

_ENABLED_KV: Final[str] = "audio_waveform_enabled"
"""kv_settings flag (``0``/``1``) used as a runtime kill-switch."""

_WORKER_NAME: Final[str] = "audio-waveform-worker"

_BATCH_LIMIT: Final[int] = 50
"""How many NULL rows the worker pulls per tick — bounds tick latency."""


async def _is_enabled() -> bool:
    """Return ``True`` unless ``audio_waveform_enabled`` is literally ``0``.

    Default-on: the kv row is created lazily, so absence == enabled.
    Mirrors the convention used by the audio merge / alt-text / auto-
    translate workers.
    """
    async with get_connection() as conn:
        value = await get_kv(conn, _ENABLED_KV)
    if value is None:
        return True
    return value.strip() != "0"


async def _list_missing() -> list[int]:
    """Return the ``id`` list of rows still missing a ``waveform_svg``.

    Bounded to :data:`_BATCH_LIMIT` so the worker tick stays cheap
    even on a fresh install with thousands of pre-existing rows.
    Ordered DESC so newly-captured segments get a thumbnail before
    the backlog drains — the operator sees fresh rows render first.
    """
    enabled = await _is_enabled()
    if not enabled:
        return []
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id
              FROM audio_segment
             WHERE waveform_svg IS NULL
             ORDER BY id DESC
             LIMIT ?
            """,
            (int(_BATCH_LIMIT),),
        )
        rows = await cursor.fetchall()
    return [int(row["id"]) for row in rows]


async def _build_one(segment_id: int) -> WaveformResult | None:
    """Render and persist a waveform thumbnail for one row.

    Returns the renderer's result dict so :class:`BackfillRunner`
    bumps its ``built`` counter on every handled row (including the
    ``missing`` / ``error`` branches — the row was processed, even
    if the column stays NULL). Any unexpected exception bubbles up
    to the runner's per-key error handler.
    """
    result = await generate_waveform(int(segment_id))
    log.info(
        "audio_waveform_worker.handled",
        segment_id=int(segment_id),
        status=result.get("status"),
        svg_length=result.get("svg_length"),
    )
    return result


async def run_audio_waveform_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`.

    The task name (``audio-waveform-worker``) lands in the heartbeat
    table so the operator can spot a stuck worker in the admin
    health view. Cancellation propagates from the lifespan shutdown
    handler.
    """
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_audio_waveform_worker",
]
