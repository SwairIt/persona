"""Periodic screen-capture worker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.app_capture_skip import is_skipped as is_capture_skipped
from app.capture import (
    capture_all_monitors,
    capture_primary_monitor,
    get_active_window,
    seconds_since_last_input,
    should_capture,
)
from app.capture.adaptive_cadence import compute_interval
from app.capture.power_state import get_power_state_async
from app.capture.session_state import is_session_locked
from app.dedup import compute_phash, find_or_create_dedup_group
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.app_overrides import lookup_override
from app.storage.db import get_connection
from app.storage.process_remap import lookup_remap
from app.storage.quiet_hours import is_quiet_now
from app.storage.repository import (
    insert_screenshot,
    log_capture_event,
    set_dedup_group_representative,
)
from app.storage.size_log import sample_today, today_bytes
from app.storage.thumbnails import save_thumbnail
from app.webhooks import dispatch_event
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.capture_loop")


async def run_capture_loop(controller: CaptureController | None = None) -> None:
    """Main capture-loop entry point. Runs until stop_event is set."""
    ctrl = controller or get_controller()
    settings = get_settings()

    async with get_connection() as conn:
        await log_capture_event(conn, "start", {"interval": settings.capture_interval_seconds})

    log.info("capture_loop.started", interval=settings.capture_interval_seconds)

    while not ctrl.stop_event.is_set():
        await beat("capture-loop")
        battery_pause = False
        battery_slowdown = False
        if settings.battery_aware_enabled:
            state = await get_power_state_async()
            if state["on_battery"]:
                percent = state["percent"]
                if percent is not None and percent < settings.battery_critical_pct:
                    log.info(
                        "capture.battery_critical",
                        percent=percent,
                        critical_pct=settings.battery_critical_pct,
                    )
                    battery_pause = True
                else:
                    log.debug(
                        "capture.battery_slowdown",
                        percent=percent,
                        multiplier=settings.battery_capture_multiplier,
                    )
                    battery_slowdown = True

        idle_observed: float | None = None
        if not battery_pause:
            try:
                idle_observed = await _single_iteration(ctrl)
            except asyncio.CancelledError:
                log.info("capture_loop.cancelled")
                raise
            except Exception as exc:
                ctrl.mark_error(str(exc))
                log.exception("capture_loop.iteration_failed", error=str(exc))
                async with get_connection() as conn:
                    await log_capture_event(conn, "error", {"error": str(exc)[:500]})

        sleep_for = ctrl.next_sleep_seconds or settings.capture_interval_seconds
        ctrl.next_sleep_seconds = None
        if settings.adaptive_cadence_enabled:
            idle_for_cadence = (
                idle_observed
                if idle_observed is not None
                else float(seconds_since_last_input())
            )
            adaptive = compute_interval(
                base_seconds=float(sleep_for),
                idle_seconds=float(idle_for_cadence),
                min_s=float(settings.adaptive_min_seconds),
                max_s=float(settings.adaptive_max_seconds),
            )
            log.debug(
                "capture.adaptive_interval",
                seconds=adaptive,
                idle_seconds=idle_for_cadence,
                base_seconds=sleep_for,
            )
            sleep_for = adaptive
        if battery_slowdown or battery_pause:
            sleep_for = sleep_for * settings.battery_capture_multiplier
        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=sleep_for,
            )
        except asyncio.TimeoutError:
            continue

    async with get_connection() as conn:
        await log_capture_event(conn, "pause", {"reason": "stop_event"})
    log.info("capture_loop.stopped")


async def _single_iteration(ctrl: CaptureController) -> float | None:  # noqa: PLR0911
    """Run one capture iteration. Returns the observed idle_seconds, or
    ``None`` if the iteration short-circuited before idle was sampled.
    """
    settings = get_settings()

    if ctrl.paused:
        return None

    async with get_connection() as conn:
        if await is_quiet_now(conn):
            ctrl.mark_idle_skip()
            return None

    if settings.lock_aware_pause_enabled and await is_session_locked():
        log.debug("capture.session_locked")
        ctrl.mark_idle_skip()
        return None

    idle_seconds = seconds_since_last_input()
    if idle_seconds > settings.idle_threshold_seconds:
        ctrl.mark_idle_skip()
        return idle_seconds

    window = await asyncio.to_thread(get_active_window)
    if window is not None and not should_capture(window.process_name):
        ctrl.mark_idle_skip()
        return idle_seconds

    if window is not None and await is_capture_skipped(window.app_name):
        log.debug("capture.app_skipped", app=window.app_name)
        ctrl.mark_idle_skip()
        return idle_seconds

    if settings.multi_monitor:
        results = await asyncio.to_thread(capture_all_monitors)
        if not results:
            return idle_seconds
    else:
        results = [await asyncio.to_thread(capture_primary_monitor)]

    for result in results:
        await _persist_capture(ctrl, result, window, idle_seconds)
    return idle_seconds


async def _persist_capture(
    ctrl: CaptureController,
    result,  # type: ignore[no-untyped-def]
    window,  # type: ignore[no-untyped-def]
    idle_seconds: float,
) -> None:
    settings = get_settings()
    phash = compute_phash(result.image)
    now = result.captured_at

    async with get_connection() as conn:
        group_id, is_new = await find_or_create_dedup_group(
            conn,
            phash=phash,
            now=now,
            threshold=settings.dedup_hamming_threshold,
        )

        if not is_new:
            ctrl.mark_dedup_skip()
            ctrl.mark_capture()
            return

        effective_app = window.app_name if window else None
        if window and window.process_name:
            remap = await lookup_remap(conn, window.process_name)
            if remap:
                effective_app = remap

        if effective_app:
            override = await lookup_override(conn, effective_app)
            if override is not None:
                ctrl.next_sleep_seconds = override

        screenshot_id = await insert_screenshot(
            conn,
            captured_at=now,
            width=result.width,
            height=result.height,
            phash=phash,
            monitor_index=result.monitor_index,
            thumbnail_path=None,
            app_name=effective_app,
            window_title=window.title if window else None,
            process_name=window.process_name if window else None,
            ocr_status="pending" if settings.ocr_enabled else "skipped",
            dedup_group_id=group_id,
        )

        await set_dedup_group_representative(conn, group_id, screenshot_id)

    save_thumb = await _should_save_thumbnail(window.app_name if window else None, now)

    thumbnail_path = None
    if save_thumb:
        thumbnail_path = await asyncio.to_thread(
            save_thumbnail,
            result.image,
            now,
            screenshot_id,
        )
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE screenshots SET thumbnail_path = ? WHERE id = ?",
                (str(thumbnail_path), screenshot_id),
            )
            await conn.commit()
            await sample_today(conn, settings.thumbnails_dir)
        await dispatch_event(
            "capture.saved",
            {
                "screenshot_id": screenshot_id,
                "app_name": window.app_name if window else None,
                "captured_at": now.isoformat(),
            },
        )

    ctrl.mark_capture()
    log.debug(
        "capture_loop.captured",
        screenshot_id=screenshot_id,
        monitor=result.monitor_index,
        app=window.app_name if window else None,
        thumb=bool(thumbnail_path),
        idle_seconds=idle_seconds,
    )


async def _should_save_thumbnail(app_name: str | None, now: datetime) -> bool:
    """Decide whether this capture earns a thumbnail.

    Smart-thumbnail logic — skip thumbnail when we already saved a recent one
    for the same app within `smart_min_gap_seconds`, OR when we've already
    blown today's size budget. Metadata (OCR text, app, window title) is
    always kept; we only skip the visual.
    """
    settings = get_settings()
    if not settings.smart_thumbnail:
        return True

    async with get_connection() as conn:
        current_bytes = await today_bytes(conn)
        budget_bytes = int(settings.daily_size_budget_mb * 1024 * 1024)
        if budget_bytes and current_bytes >= budget_bytes:
            return False

        if app_name is None:
            return True

        cutoff_iso = (now - _timedelta_seconds(settings.smart_min_gap_seconds)).isoformat()
        cursor = await conn.execute(
            "SELECT 1 FROM screenshots WHERE app_name = ? AND thumbnail_path IS NOT NULL "
            "AND captured_at >= ? LIMIT 1",
            (app_name, cutoff_iso),
        )
        row = await cursor.fetchone()
        if row is not None:
            return False
    return True


def _timedelta_seconds(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


def _now() -> datetime:
    return datetime.now(timezone.utc)
