"""Video-conference detector for the capture loop's smart-pause feature.

The capture loop calls :func:`detect_meeting` once per iteration with the
currently-active window's ``app_name`` plus the names of the last few
windows it has seen. When a match against any of the hard-coded
patterns (``zoom``, ``teams``, ``meet``, ``discord``, ``telegram``,
``webex``, ``skype``) is found AND the kv-flag
``meeting_pause_enabled`` is set to ``"1"``, the function returns
``in_meeting=True`` and the loop skips the capture iteration — the
same kill-path that the master ``capture_screens_disabled`` switch
uses.

The function is intentionally synchronous and side-effect-free: it does
not touch the database, the filesystem, or any I/O. The caller is
responsible for the kv read (so the hot path can amortise it across
many calls) and for recording transitions via :func:`record_event_start`
and :func:`record_event_end`. Keeping detection pure makes unit-testing
trivial and means the loop's inner iteration stays cheap.

Pattern matching is case-insensitive substring matching — many of the
target apps publish their window title under a slightly different name
than their process (``zoom.us`` vs ``Zoom Meetings``, ``Microsoft
Teams (work or school)`` vs ``Teams``), and substring matching covers
both without an alias table to maintain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.meeting_detector")


# Hard-coded substrings, all lowercase. Matched against ``app_name``
# (which the capture loop fills from the active window's display title)
# with simple ``in`` containment. Order is irrelevant; the first match
# wins purely so we have *something* deterministic to return for the
# UI to render — both apps trigger the pause either way.
_PATTERNS: tuple[str, ...] = (
    "zoom",
    "teams",
    "meet.google",
    "meet",
    "discord",
    "telegram",
    "webex",
    "skype",
)


class DetectResult(TypedDict):
    """Result envelope for :func:`detect_meeting`."""

    in_meeting: bool
    matched_app: str | None
    matched_pattern: str | None


def detect_meeting(
    active_app_name: str | None,
    recent_app_names: list[str],
    *,
    enabled: bool = True,
) -> DetectResult:
    """Return whether the user is currently in a video meeting.

    Parameters
    ----------
    active_app_name:
        The display name of the currently-active window, or ``None`` if
        the capture loop could not resolve one (e.g. on the lock
        screen). Checked first because the active window is the
        highest-confidence signal.
    recent_app_names:
        Display names of the last few windows seen by the loop (the
        caller passes the most recent first, oldest last). Only the
        first three entries are inspected — beyond that the signal is
        too stale to be useful and risks false positives long after
        the user actually left the meeting.
    enabled:
        Pre-resolved value of the kv flag ``meeting_pause_enabled``.
        Passed explicitly so the caller can read it once per iteration
        rather than this function round-tripping to SQLite. When
        ``False`` the function short-circuits with a negative result
        regardless of the inputs.

    Returns
    -------
    DetectResult
        A ``{in_meeting, matched_app, matched_pattern}`` dict. The
        ``matched_*`` fields are ``None`` when ``in_meeting`` is
        ``False`` (either because nothing matched or because the
        feature is disabled).
    """
    if not enabled:
        return {"in_meeting": False, "matched_app": None, "matched_pattern": None}

    # The order matters: the active window is the strongest signal, so
    # try it first; only fall back to the recent-window ring if the
    # active window itself does not match. This keeps the diagnostic
    # ``matched_app`` field pointing at the most authoritative source.
    candidates: list[str] = []
    if active_app_name:
        candidates.append(active_app_name)
    for name in recent_app_names[:3]:
        if name and name != active_app_name:
            candidates.append(name)

    for candidate in candidates:
        lowered = candidate.lower()
        for pattern in _PATTERNS:
            if pattern in lowered:
                log.debug(
                    "meeting_detector.match",
                    app=candidate,
                    pattern=pattern,
                )
                return {
                    "in_meeting": True,
                    "matched_app": candidate,
                    "matched_pattern": pattern,
                }

    return {"in_meeting": False, "matched_app": None, "matched_pattern": None}


async def record_event_start(
    conn: aiosqlite.Connection,
    *,
    app_name: str,
    pattern: str,
) -> int:
    """Insert a new ``meeting_event`` row for a freshly-detected meeting.

    Returns the row id so the caller can pass it to
    :func:`record_event_end` when the meeting ends. Idempotent at the
    call site is the caller's responsibility — the helper unconditionally
    inserts.
    """
    cursor = await conn.execute(
        "INSERT INTO meeting_event (app_name, pattern) VALUES (?, ?)",
        (app_name, pattern),
    )
    await conn.commit()
    inserted_id = cursor.lastrowid
    log.info(
        "meeting_detector.event_started",
        app=app_name,
        pattern=pattern,
        event_id=inserted_id,
    )
    return int(inserted_id or 0)


async def record_event_end(conn: aiosqlite.Connection, event_id: int) -> None:
    """Stamp ``ended_at`` on the meeting-event row created when we entered."""
    await conn.execute(
        "UPDATE meeting_event SET ended_at = datetime('now') WHERE id = ? AND ended_at IS NULL",
        (event_id,),
    )
    await conn.commit()
    log.info("meeting_detector.event_ended", event_id=event_id)
