"""Periodic screen-capture worker."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime, timedelta

from app.app_capture_skip import is_skipped as is_capture_skipped
from app.capture import (
    capture_all_monitors,
    capture_primary_monitor,
    get_active_window,
    seconds_since_last_input,
    should_capture,
)
from app.capture.adaptive_cadence import compute_interval
from app.capture.meeting_detector import (
    detect_meeting,
    record_event_end,
    record_event_start,
)
from app.capture.power_state import get_power_state_async
from app.capture.session_state import is_session_locked
from app.capture_blocklist import (
    find_matching_rule as find_blocklist_match,
)
from app.capture_blocklist import (
    is_blocked as is_capture_regex_blocked,
)
from app.capture_blocklist import (
    list_active_rules as list_blocklist_rules,
)
from app.dedup import compute_phash, find_or_create_dedup_group
from app.focus import current_session as current_focus_session
from app.focus_blocklist import is_blocked as is_focus_blocked
from app.focus_whitelist import is_focus_allowed as is_focus_whitelist_allowed
from app.focus_whitelist import record_skip as record_focus_whitelist_skip
from app.logging_setup import get_logger
from app.privacy_mode import is_private_window as is_privacy_match
from app.privacy_mode import record_skip as record_privacy_skip
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
rate_guard_log = get_logger("persona.capture.rate_guard")

# v1.19 — smart-pause meeting detector ring buffer + active-meeting cursor.
#
# The detector wants the *recently seen* app names, not just the
# currently-active one — when the user alt-tabs from Zoom to their
# notes for a second the active window momentarily stops matching but
# the meeting is still on. A length-3 deque covers that without
# growing unbounded.
#
# ``_active_meeting_event_id`` tracks the row id of the currently-open
# ``meeting_event`` row so we know which row to stamp ``ended_at`` on
# when the meeting ends. ``None`` when we are not currently paused
# for a meeting.
_recent_app_names: deque[str] = deque(maxlen=3)
_active_meeting_event_id: int | None = None


async def run_capture_loop(controller: CaptureController | None = None) -> None:  # noqa: PLR0912, PLR0915
    """Main capture-loop entry point. Runs until stop_event is set."""
    ctrl = controller or get_controller()
    settings = get_settings()

    async with get_connection() as conn:
        await log_capture_event(conn, "start", {"interval": settings.capture_interval_seconds})

    log.info("capture_loop.started", interval=settings.capture_interval_seconds)

    while not ctrl.stop_event.is_set():
        await beat("capture-loop")
        # v1.25 — single-resolver replaces the v1.17 dual-key dance.
        # ``get_effective_many`` reads kv first, falls back to Settings,
        # so the wizard (which writes kv) and any direct env override
        # both work without the call-site needing to know which is which.
        # The legacy ``capture_interval_seconds_live`` kv key is still
        # honoured for back-compat (writers may have stale UI form data).
        live_interval: float | None = None
        try:
            from app.settings.effective import (  # noqa: PLC0415
                _coerce_bool,
                get_effective_many,
            )

            values = await get_effective_many(
                [
                    "capture_screens_disabled",
                    "capture_interval_seconds",
                    "capture_interval_seconds_live",
                ]
            )
            # Two kv keys can carry the live interval — the canonical
            # one and the legacy ``_live`` from v1.17. Try canonical first.
            for key in ("capture_interval_seconds", "capture_interval_seconds_live"):
                raw = values.get(key)
                if raw is None or isinstance(raw, bool):
                    continue
                try:
                    live_interval = max(0.5, min(60.0, float(str(raw))))
                    break
                except (TypeError, ValueError):
                    continue
            if _coerce_bool(values.get("capture_screens_disabled")):
                sleep_for = live_interval or settings.capture_interval_seconds
                try:
                    await asyncio.wait_for(
                        ctrl.stop_event.wait(),
                        timeout=sleep_for,
                    )
                except TimeoutError:
                    continue
                continue
        except Exception as exc:
            log.debug("capture_loop.live_kv_check_failed", error=str(exc))

        # v1.19 — smart-pause for Zoom/Teams/Meet/Discord/etc.
        #
        # Read the kv flag in the same iteration (cheap: same SQLite
        # connection pool we just used for the screens_kill check).
        # When ``meeting_pause_enabled=1`` AND the detector matches
        # the active/recent windows against its hard-coded pattern
        # list, skip the iteration — mirrors the screens_kill
        # short-circuit above so the sleep schedule stays identical.
        #
        # Transitions (entered / left a meeting) are logged at INFO
        # and persisted to ``meeting_event``; the per-tick steady
        # state is silent so we don't spam the log when a meeting
        # runs for an hour.
        try:
            meeting_active = await _check_meeting_pause()
        except Exception as exc:
            log.debug("capture_loop.meeting_check_failed", error=str(exc))
            meeting_active = False
        if meeting_active:
            sleep_for = live_interval or settings.capture_interval_seconds
            try:
                await asyncio.wait_for(
                    ctrl.stop_event.wait(),
                    timeout=sleep_for,
                )
            except TimeoutError:
                continue
            continue

        # v1.40 — privacy-mode sentinel. Sample the active window once
        # and, if it matches one of the hard-coded privacy patterns
        # (Incognito, password manager, banking, ...), record a hashed
        # skip event and short-circuit BEFORE ``_single_iteration``
        # writes any metadata row. Stricter than the regex blocklist
        # above: that path still logs the matched window title in the
        # structlog stream, this path keeps only a truncated sha256.
        # Failure modes never stop capture by design — a broken
        # privacy probe must not silently halt the loop.
        try:
            privacy_window = await asyncio.to_thread(get_active_window)
            matched, pattern = is_privacy_match(
                privacy_window.app_name if privacy_window is not None else None,
                privacy_window.title if privacy_window is not None else None,
            )
        except Exception as exc:
            log.debug("capture_loop.privacy_check_failed", error=str(exc))
            matched, pattern, privacy_window = False, None, None
        if matched and pattern is not None:
            await record_privacy_skip(
                privacy_window.app_name if privacy_window is not None else None,
                privacy_window.title if privacy_window is not None else None,
                pattern,
            )
            sleep_for = live_interval or settings.capture_interval_seconds
            try:
                await asyncio.wait_for(
                    ctrl.stop_event.wait(),
                    timeout=sleep_for,
                )
            except TimeoutError:
                continue
            continue

        # v1.47 — focus-session app whitelist. Inverse of focus_blocklist:
        # while a focus_session is active, skip any shot whose active
        # window is NOT on the whitelist. Empty whitelist means "open
        # mode" and the helper returns ``True`` so this branch is a
        # no-op — paying for the helper is one indexed SQLite read,
        # cheap enough to keep on the hot path. Mirrors the privacy
        # hook style above: failure modes (DB error, lookup failure)
        # never stop capture, by design.
        try:
            focus_window = (
                privacy_window
                if privacy_window is not None
                else await asyncio.to_thread(get_active_window)
            )
            focus_active = await current_focus_session()
            if focus_active is not None:
                focus_app = focus_window.app_name if focus_window is not None else None
                if not await is_focus_whitelist_allowed(focus_app):
                    await record_focus_whitelist_skip(
                        focus_app,
                        focus_active["id"],
                    )
                    sleep_for = live_interval or settings.capture_interval_seconds
                    try:
                        await asyncio.wait_for(
                            ctrl.stop_event.wait(),
                            timeout=sleep_for,
                        )
                    except TimeoutError:
                        continue
                    continue
        except Exception as exc:
            log.debug("capture_loop.focus_whitelist_check_failed", error=str(exc))

        rate_pause = await _enforce_rate_guard()
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
        if not battery_pause and not rate_pause:
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

        base_interval = live_interval if live_interval is not None else settings.capture_interval_seconds
        sleep_for = ctrl.next_sleep_seconds or base_interval
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
        except TimeoutError:
            continue

    async with get_connection() as conn:
        await log_capture_event(conn, "pause", {"reason": "stop_event"})
    log.info("capture_loop.stopped")


async def _single_iteration(ctrl: CaptureController) -> float | None:  # noqa: PLR0911, PLR0912
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

    # v1.21 — regex blocklist. Stricter sibling of ``app_capture_skip``.
    # Extracted into a helper so this iteration body stays under the
    # branch-count lint cap.
    if window is not None and await _regex_blocklist_blocks(window):
        ctrl.mark_idle_skip()
        return idle_seconds

    if window is not None:
        # v0.85 distraction blocker: only consult the focus blocklist when a
        # session is actually running. Probing ``focus_session`` first keeps
        # the hot path cheap when no session is active (the common case) —
        # the indexed ``ended_at IS NULL`` lookup is O(log n) and the
        # blocklist query is skipped entirely.
        active_session = await current_focus_session()
        if active_session is not None and await is_focus_blocked(window.app_name):
            log.debug(
                "capture.focus_blocked",
                app=window.app_name,
                session_id=active_session["id"],
            )
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


async def _regex_blocklist_blocks(window: object) -> bool:
    """Consult the regex blocklist for the current foreground ``window``.

    Returns ``True`` when at least one enabled rule matches the active
    app name or window title, ``False`` otherwise. Failure modes (DB
    error, regex compile error inside the helper) never raise — a
    broken blocklist must not stop the capture loop, so any exception
    here downgrades to "not blocked" and is logged at DEBUG.

    The helper exists to keep :func:`_single_iteration` under the
    ``PLR0912`` branch-count cap; inlining the same code costs three
    extra branches in the caller.
    """
    # ``window`` is typed as ``object`` to avoid a hard import-time
    # dependency on the ``ActiveWindow`` dataclass here — the caller
    # already verified the value is not ``None`` before invoking us.
    app_name = getattr(window, "app_name", None)
    title = getattr(window, "title", None)
    try:
        async with get_connection() as conn:
            rules = await list_blocklist_rules(conn)
    except Exception as exc:
        log.debug("capture_loop.blocklist_load_failed", error=str(exc))
        return False
    if not rules or not is_capture_regex_blocked(app_name, title, rules):
        return False
    matched = find_blocklist_match(app_name, title, rules)
    log.info(
        "capture.blocked_by_regex",
        app=app_name,
        title=(title or "")[:80],
        pattern=matched[0].pattern if matched else None,
        field=matched[1] if matched else None,
    )
    return True


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
        # v1.13 — count toward the daily byte budget. The enforcer (§6 of
        # STORAGE_BUDGET_DESIGN.md) reads this bucket to decide whether
        # to raise the throttle level on subsequent captures. Failures
        # MUST NOT break capture — wrap in try/except.
        try:
            from pathlib import Path as _Path  # noqa: PLC0415

            from app import budget as _budget  # noqa: PLC0415

            written = _Path(thumbnail_path).stat().st_size
            await _budget.add_bytes("thumbnails", written)
        except Exception as exc:
            log.debug("capture_loop.budget_bump_failed", error=str(exc))
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
    return datetime.now(UTC)


async def _check_meeting_pause() -> bool:
    """Update the recent-app ring and decide whether to pause for a meeting.

    Returns ``True`` when the smart-pause is engaged this iteration
    (caller should skip capture and sleep). Returns ``False`` when the
    feature is disabled, when no pattern matched, or when sampling the
    active window failed — failure modes never block capture, by
    design: a broken detector must not silently stop screenshots.

    Side effects:

    * Pushes the current active app name into ``_recent_app_names``.
    * Inserts a ``meeting_event`` row on entering a meeting (with the
      row id cached in ``_active_meeting_event_id``).
    * Stamps ``ended_at`` on that row when the meeting ends.
    """
    global _active_meeting_event_id  # noqa: PLW0603

    from app.storage.repository import get_kv  # noqa: PLC0415

    async with get_connection() as conn:
        flag = await get_kv(conn, "meeting_pause_enabled")
    enabled = (flag or "0").strip() == "1"

    # Even when disabled we still sample the window so that, the
    # moment the user flips the flag on, the ring buffer is already
    # warm — otherwise the first three ticks after enabling would be
    # blind.
    window = await asyncio.to_thread(get_active_window)
    active_app = window.app_name if window is not None else None
    if active_app:
        _recent_app_names.append(active_app)

    result = detect_meeting(
        active_app,
        list(_recent_app_names),
        enabled=enabled,
    )

    if result["in_meeting"]:
        if _active_meeting_event_id is None:
            matched_app = result["matched_app"] or "unknown"
            matched_pattern = result["matched_pattern"] or "unknown"
            async with get_connection() as conn:
                _active_meeting_event_id = await record_event_start(
                    conn,
                    app_name=matched_app,
                    pattern=matched_pattern,
                )
            log.info(
                "capture_loop.meeting_entered",
                app=matched_app,
                pattern=matched_pattern,
            )
        return True

    # Not in a meeting right now — close out the previous one if any.
    if _active_meeting_event_id is not None:
        closed_id = _active_meeting_event_id
        _active_meeting_event_id = None
        async with get_connection() as conn:
            await record_event_end(conn, closed_id)
        log.info("capture_loop.meeting_left", event_id=closed_id)
    return False


async def _enforce_rate_guard() -> bool:
    """Capture-rate guard for v0.90.

    Counts ``screenshots`` rows whose ``captured_at`` falls inside the
    trailing 60-minute window and consults the two configurable
    thresholds:

    * ``capture_rate_warn_per_hour`` — when reached, log a warning so
      an operator can investigate runaway capture.
    * ``capture_rate_pause_per_hour`` — when reached AND non-zero,
      return ``True`` so the caller skips the iteration entirely. Zero
      disables the pause arm of the guard.

    Returns ``True`` only when the pause threshold fires; the warning
    arm never affects scheduling.
    """
    settings = get_settings()
    warn_threshold = settings.capture_rate_warn_per_hour
    pause_threshold = settings.capture_rate_pause_per_hour
    if warn_threshold <= 0 and pause_threshold <= 0:
        return False

    cutoff_iso = (_now() - timedelta(hours=1)).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM screenshots WHERE captured_at >= ?",
            (cutoff_iso,),
        )
        row = await cursor.fetchone()
    count = int(row[0]) if row is not None else 0

    pause = pause_threshold > 0 and count >= pause_threshold
    if pause:
        rate_guard_log.warning(
            "capture.rate_pause",
            count=count,
            pause_threshold=pause_threshold,
            warn_threshold=warn_threshold,
            window_seconds=3600,
        )
    elif warn_threshold > 0 and count >= warn_threshold:
        rate_guard_log.warning(
            "capture.rate_warn",
            count=count,
            warn_threshold=warn_threshold,
            pause_threshold=pause_threshold,
            window_seconds=3600,
        )
    return pause
