"""Background worker that auto-translates fresh voice segments (v1.18).

The worker polls every :data:`POLL_INTERVAL_SECONDS` (15 min). On each
tick it:

1. Reads the gate kv row ``audio_auto_translate_enabled`` — when the
   value is anything other than ``"1"`` the tick is a no-op.
2. Resolves the user's UI language (``app.i18n.get_ui_language``) as
   the translation target.
3. Selects the 3 most recent rows from ``audio_segment`` where
   ``transcript_translated IS NULL AND transcript IS NOT NULL`` ordered
   by ``captured_at DESC``.
4. Calls :func:`app.audio.auto_translate.translate_segment` on each in
   sequence (so we never fire 3 concurrent LLM calls — that would burn
   3x the rate-limit budget for no UX win).
5. Heartbeats once per tick via :func:`app.workers.heartbeat.beat`.

Per-row failures bubble up as status strings inside
:class:`~app.audio.auto_translate.TranslateResult` and are logged at
INFO — they never crash the loop. Only ``asyncio.CancelledError` exits
the worker.

The lifespan-task wiring lives in the coordinator module — this file
deliberately exposes only :func:`run_auto_translate_worker` and never
modifies the lifespan tasks list itself.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.audio.auto_translate import translate_segment
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers.heartbeat import beat

log = get_logger("persona.workers.auto_translate")

POLL_INTERVAL_SECONDS: float = 900.0
"""15 minutes. Translation is a paid LLM call against the user's BYO
key, so polling more aggressively than this just burns rate-limit
budget without a UX win — fresh segments accrue minutes-to-hours apart,
not seconds."""

_HEARTBEAT_NAME: str = "auto-translate-worker"
"""Surfaced on the ``/admin/health`` dashboard. Underscored worker
names are normalised elsewhere; keeping the dashed form consistent
with the other workers (``audio-retention-worker``, …)."""

_KV_ENABLED: str = "audio_auto_translate_enabled"
"""kv_settings row that gates the worker. Default OFF — the user opts
in via the settings page so we never silently bill against their BYO
key on the very first boot after an upgrade."""

_BATCH_LIMIT: int = 3
"""Per-tick row cap. Three is enough to make steady progress on a
backlog (12 segments / hour) while keeping the worst-case tick under a
few API calls — the user might be on a small free tier."""


async def _gate_enabled() -> bool:
    """Return ``True`` when the kv row is set to ``"1"``.

    A missing row, an empty value, or anything else returns ``False`` so
    a fresh install stays opted-out by default. Read failures are
    swallowed and treated as opted-out — a transient DB error must
    never accidentally enable a paid feature.
    """
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, _KV_ENABLED)
    except Exception as exc:
        log.warning("auto_translate.gate.read_failed", error=str(exc))
        return False
    if raw is None:
        return False
    return raw.strip() == "1"


async def _resolve_target_lang() -> str:
    """Return the UI language to translate INTO.

    Preference order:

    1. :func:`app.i18n.get_ui_language` — the canonical resolver used
       by the templates. Wrapped in try/except so an import-time bug
       can never crash the worker.
    2. kv row ``ui_language`` directly — same source the function
       above reads from, used as a fallback so the worker still
       degrades gracefully when the i18n module fails to import.
    3. Hard default ``"en"`` — matches :data:`app.i18n.DEFAULT_LANGUAGE`.
    """
    try:
        from app.i18n import get_ui_language  # noqa: PLC0415 — keep import lazy
    except Exception as exc:
        log.warning("auto_translate.i18n.import_failed", error=str(exc))
        get_ui_language = None  # type: ignore[assignment]

    if get_ui_language is not None:
        try:
            value = get_ui_language()
        except Exception as exc:
            log.warning("auto_translate.i18n.read_failed", error=str(exc))
        else:
            if value:
                return value

    # Fallback path: read the kv row directly.
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, "ui_language")
    except Exception as exc:
        log.warning("auto_translate.kv.ui_language_failed", error=str(exc))
        return "en"
    if raw is None:
        return "en"
    cleaned = raw.strip().lower()
    return cleaned or "en"


async def _select_pending(limit: int) -> list[int]:
    """Return up to ``limit`` segment ids awaiting translation.

    Ordered ``captured_at DESC`` so the user sees the freshest segments
    translated first — historical backfill is a secondary concern and
    can be triggered manually from the admin route.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id FROM audio_segment "
                "WHERE transcript_translated IS NULL "
                "  AND transcript IS NOT NULL "
                "  AND TRIM(transcript) <> '' "
                "ORDER BY captured_at DESC "
                "LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
    except Exception as exc:
        log.exception("auto_translate.select_failed", error=str(exc))
        return []
    return [int(row["id"]) for row in rows]


async def _tick() -> dict[str, Any]:
    """One pass: gate-check, resolve target, translate up to 3 rows."""
    if not await _gate_enabled():
        return {"skipped": "disabled"}

    target_lang = await _resolve_target_lang()
    seg_ids = await _select_pending(_BATCH_LIMIT)
    if not seg_ids:
        return {"target_lang": target_lang, "translated": 0, "pending": 0}

    counters: dict[str, int] = {
        "ok": 0,
        "already_target": 0,
        "no_text": 0,
        "missing_config": 0,
        "missing_segment": 0,
        "error": 0,
    }

    for seg_id in seg_ids:
        try:
            result = await translate_segment(seg_id, target_lang)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "auto_translate.row.crashed",
                seg_id=seg_id,
                error=str(exc),
            )
            counters["error"] += 1
            continue
        status = result["status"]
        counters[status] = counters.get(status, 0) + 1
        if status == "missing_config":
            # No point retrying the other rows on this tick — they'd
            # all hit the same gate. Exit early and let the next tick
            # try again after the operator (hopefully) configures a key.
            log.info("auto_translate.tick.missing_config", seg_id=seg_id)
            break

    return {
        "target_lang": target_lang,
        "translated": counters["ok"],
        **counters,
    }


async def run_auto_translate_worker() -> None:
    """Poll loop for the auto-translate worker.

    Stops only on :class:`asyncio.CancelledError` (raised by the
    lifespan shutdown). Every other exception inside :func:`_tick` is
    swallowed via :func:`logging.Logger.exception` so a transient
    backend error cannot crash the loop.
    """
    log.info(
        "auto_translate.worker.starting",
        poll_seconds=POLL_INTERVAL_SECONDS,
        batch_limit=_BATCH_LIMIT,
    )
    while True:
        await beat(_HEARTBEAT_NAME)
        try:
            stats = await _tick()
            if stats:
                log.info("auto_translate.tick", **stats)
        except asyncio.CancelledError:
            log.info("auto_translate.worker.cancelled")
            raise
        except Exception as exc:
            log.exception("auto_translate.tick_failed", error=str(exc))

        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("auto_translate.worker.cancelled")
            raise


__all__ = ["POLL_INTERVAL_SECONDS", "run_auto_translate_worker"]
