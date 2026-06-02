"""Background worker that captures opt-in clipboard history.

Polls the OS clipboard every :data:`POLL_INTERVAL_SECONDS` and inserts
each *new* text snippet into ``clipboard_event``. "New" means the SHA-256
hash of the text differs from the last seen — Windows reports the same
clipboard buffer over and over and we don't want a row per poll.

Privacy posture:
    * Hard-gated by ``clipboard_history_enabled`` (default ``False``).
      If the setting is off at startup the worker awaits ``stop_event``
      and never reads the clipboard at all.
    * The setting is re-checked every iteration so the user can flip
      capture off without restarting the app.
    * Redaction rules from :mod:`app.redaction` are applied **before**
      the row is inserted, so emails / API keys / etc. are masked.
    * We log structlog events but **never** the raw text — only the
      character length and the hash prefix.

Cooperates with the same pause signals the capture loop honours:
    * ``controller.paused`` — skip iteration.
    * session lock (``app.capture.session_state.is_session_locked``)
      when ``settings.lock_aware_pause_enabled``.
    * idle (``seconds_since_last_input > idle_threshold_seconds``).
"""

from __future__ import annotations

import asyncio

from app.capture.clipboard import hash_text, read_clipboard_text
from app.capture.idle import seconds_since_last_input
from app.capture.session_state import is_session_locked
from app.capture.window import get_active_window
from app.logging_setup import get_logger
from app.redaction import apply_redaction
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.clipboard")

POLL_INTERVAL_SECONDS = 2.0
MIN_TEXT_LENGTH = 3


async def run_clipboard_worker(controller: CaptureController | None = None) -> None:
    """Continuously sample the clipboard while history is enabled.

    Runs until ``controller.stop_event`` fires. If the feature is off the
    worker waits on the stop event without polling — flipping the setting
    requires a restart in that case (consistent with embeddings_worker).
    """
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.clipboard_history_enabled:
        log.info("clipboard_worker.disabled")
        await ctrl.stop_event.wait()
        return

    log.info("clipboard_worker.started", poll_seconds=POLL_INTERVAL_SECONDS)

    last_hash: str | None = await _load_last_hash()

    while not ctrl.stop_event.is_set():
        await beat("clipboard-worker")
        try:
            last_hash = await _poll_once(ctrl, last_hash)
        except asyncio.CancelledError:
            log.info("clipboard_worker.cancelled")
            raise
        except Exception as exc:
            log.exception("clipboard_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info("clipboard_worker.stopped")


async def _poll_once(  # noqa: PLR0911 — guard rails return last_hash unchanged at each gate
    ctrl: CaptureController,
    last_hash: str | None,
) -> str | None:
    """Run one clipboard read + insert iteration. Returns updated last_hash."""
    settings = get_settings()

    # Re-read settings every tick so the user can flip the toggle live.
    if not settings.clipboard_history_enabled:
        return last_hash

    if ctrl.paused:
        return last_hash

    if settings.lock_aware_pause_enabled and await is_session_locked():
        return last_hash

    idle_seconds = seconds_since_last_input()
    if idle_seconds > settings.idle_threshold_seconds:
        return last_hash

    text = await read_clipboard_text()
    if text is None:
        return last_hash

    if len(text) <= MIN_TEXT_LENGTH - 1:
        return last_hash

    raw_hash = hash_text(text)
    if raw_hash == last_hash:
        return last_hash

    cleaned, masks = await apply_redaction(text)
    length = len(text)

    app_name: str | None = None
    try:
        window = await asyncio.to_thread(get_active_window)
        if window is not None:
            app_name = window.app_name or None
    except Exception as exc:
        log.debug("clipboard_worker.window_probe_failed", error=str(exc))

    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO clipboard_event (text, length, app_name, hash) "
            "VALUES (?, ?, ?, ?)",
            (cleaned, length, app_name, raw_hash),
        )
        await conn.commit()

    log.info(
        "clipboard_worker.captured",
        length=length,
        masks_applied=masks,
        app=app_name,
        hash_prefix=raw_hash[:8],
    )
    return raw_hash


async def _load_last_hash() -> str | None:
    """Seed ``last_hash`` from the most recent stored row, if any.

    Avoids re-inserting an identical snippet across restarts when the
    user has not touched the clipboard since shutdown.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT hash FROM clipboard_event "
                "ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
    except Exception as exc:
        log.debug("clipboard_worker.seed_failed", error=str(exc))
        return None
    if row is None:
        return None
    return str(row["hash"])
