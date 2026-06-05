"""Quick-actions panel — one-click buttons for common operator tasks.

Four surfaces, all driven off the same catalogue in
:mod:`app.quick_actions`:

* ``GET  /quick-actions``                — full-page HTML grid that
                                           extends ``base.html`` and
                                           sets ``active_nav=timeline``.
* ``GET  /widget/quick-actions``         — standalone HTML fragment for
                                           HTMX embed (no ``base.html``).
* ``POST /api/quick-actions/{action_id}/run``
                                         — execute the action by
                                           direct DB / controller call.
                                           Body: optional JSON
                                           ``{"input": "..."}`` used by
                                           actions that consume free
                                           text (e.g. ``add_note``).
* ``GET  /api/quick-actions.json``       — return the raw catalogue so
                                           external tooling can render
                                           its own panel.

Design choices
==============

* **No HTTP fan-out.** ``/api/.../run`` invokes the action via the
  storage layer directly (parametrised SQL, no string concatenation)
  rather than turning around and POSTing to the canonical URL listed
  in the catalogue. That keeps the call non-recursive even when this
  router shares a process with the canonical endpoint.
* **Catalogue is the contract.** The run endpoint switches on the
  ``action_id`` and refuses anything not in :data:`ACTIONS`. New
  actions land first in :mod:`app.quick_actions`; the run endpoint
  grows a branch in the same change.
* **Optional input.** Actions that don't need input still accept the
  body (it's ignored). Actions that need it (``add_note``) reject an
  empty body with a 400.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from app.focus_profiles import install_preset
from app.logging_setup import get_logger
from app.quick_actions import ACTIONS, QuickAction, find_action
from app.storage.db import get_connection
from app.storage.notes import insert_inbox_note
from app.storage.repository import get_kv, set_kv
from app.storage.time import iso
from app.web.templates_engine import templates

router = APIRouter(tags=["quick-actions"])
log = get_logger("persona.web.quick_actions")

#: Hours added to every undismissed reminder's ``due_at`` when the
#: ``snooze_all_reminders`` action fires. Mirrors the default snooze
#: copy on the AI-reminders page; deliberately not user-configurable
#: from the panel because the panel is a *one-click* surface.
_SNOOZE_ALL_HOURS: Final[int] = 4

#: kv row consulted by the audio worker. Must match the constant in
#: :mod:`app.web.routes.mic_toggle`. Imported by value (not by name)
#: because that module exposes it as a private symbol; duplicating it
#: here is intentional — if the worker ever renames the row, both
#: modules need a coordinated change.
_KV_MIC_PAUSED: Final[str] = "audio_capture_paused_live"

#: Preset name the ``new_focus_pair_coding`` action installs. Must
#: match an entry in :data:`app.focus_profiles.PRESET_PROFILES`.
_PAIR_CODING_PRESET: Final[str] = "Pair Coding"


class _RunPayload(BaseModel):
    """JSON body for the run endpoint. All fields optional."""

    input: str | None = None


@router.get("/quick-actions", response_class=HTMLResponse)
async def quick_actions_page(request: Request) -> HTMLResponse:
    """Render the full quick-actions grid."""
    log.info("quick_actions.page", count=len(ACTIONS))
    return templates.TemplateResponse(
        request,
        "quick_actions.html",
        {
            "title": "Quick actions",
            "active_nav": "timeline",
            "actions": ACTIONS,
        },
    )


@router.get("/widget/quick-actions", response_class=HTMLResponse)
async def quick_actions_widget(request: Request) -> HTMLResponse:
    """Return a standalone HTML fragment for HTMX embed.

    The fragment renders the same button grid as the full page but
    without ``base.html`` — suitable for ``hx-get="/widget/quick-actions"``
    on the timeline, dashboard, or any other host page.
    """
    return templates.TemplateResponse(
        request,
        "_quick_actions_widget.html",
        {"actions": ACTIONS},
    )


@router.get("/api/quick-actions.json")
async def quick_actions_catalogue() -> JSONResponse:
    """Return the raw catalogue. Stable shape for external tooling."""
    return JSONResponse({"ok": True, "actions": list(ACTIONS)})


@router.post("/api/quick-actions/{action_id}/run")
async def quick_actions_run(
    action_id: str,
    payload: _RunPayload | None = None,
) -> JSONResponse:
    """Execute the action keyed by ``action_id``.

    Returns a 200 with ``{ok: true, action_id, message}`` on success,
    a 404 if the slug is not in the catalogue, a 400 if the action
    needs input the caller didn't supply, and a 501 if the action is
    catalogued but not yet wired to a direct executor in this module.
    """
    action = find_action(action_id)
    if action is None:
        log.info("quick_actions.run.unknown", action_id=action_id)
        raise HTTPException(status_code=404, detail="Unknown action")
    user_input = (payload.input if payload is not None else None) or None

    message = await _dispatch(action, user_input)
    log.info("quick_actions.run.ok", action_id=action_id)
    return JSONResponse(
        {"ok": True, "action_id": action_id, "message": message}
    )


async def _dispatch(action: QuickAction, user_input: str | None) -> str:
    """Run the action by direct call; return the success message."""
    action_id = action["action_id"]
    if action_id == "add_note":
        return await _run_add_note(user_input, action["success_message"])
    if action_id == "pin_last_shot":
        return await _run_pin_last_shot(action["success_message"])
    if action_id == "snooze_all_reminders":
        return await _run_snooze_all_reminders(action["success_message"])
    if action_id == "capture_now":
        return await _run_capture_now(action["success_message"])
    if action_id == "mic_toggle":
        return await _run_mic_toggle(action["success_message"])
    if action_id == "new_focus_pair_coding":
        return await _run_install_pair_coding(action["success_message"])
    # Catalogued but not (yet) wired here — surface explicitly instead
    # of silently no-opping so a typo'd dispatch is loud.
    raise HTTPException(
        status_code=501,
        detail=f"action {action_id!r} catalogued but not executable",
    )


async def _run_add_note(user_input: str | None, success_message: str) -> str:
    """Insert a free-text note into the inbox."""
    body = (user_input or "").strip()
    if not body:
        raise HTTPException(
            status_code=400,
            detail="input is required for add_note",
        )
    async with get_connection() as conn:
        note_id = await insert_inbox_note(conn, body=body, source="quick_actions")
    log.info("quick_actions.add_note", note_id=note_id, length=len(body))
    return f"{success_message} (#{note_id})"


async def _run_pin_last_shot(success_message: str) -> str:
    """Pin the highest-id screenshot if it isn't already pinned."""
    now_iso = iso(datetime.now(UTC))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM screenshots ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="No screenshots to pin",
            )
        latest_id = int(row["id"])
        update = await conn.execute(
            "UPDATE screenshots SET pinned_at = ? "
            "WHERE id = ? AND pinned_at IS NULL",
            (now_iso, latest_id),
        )
        await conn.commit()
        was_pinned = int(update.rowcount) == 1
    log.info(
        "quick_actions.pin_last_shot",
        latest_id=latest_id,
        was_pinned=was_pinned,
    )
    suffix = "" if was_pinned else " (was already pinned)"
    return f"{success_message}{suffix}"


async def _run_snooze_all_reminders(success_message: str) -> str:
    """Push every undismissed reminder's ``due_at`` by 4 hours."""
    new_due = iso(datetime.now(UTC) + timedelta(hours=_SNOOZE_ALL_HOURS))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE ai_reminder SET due_at = ? "
            "WHERE dismissed_at IS NULL",
            (new_due,),
        )
        await conn.commit()
        affected = int(cursor.rowcount)
    log.info("quick_actions.snooze_all", affected=affected, new_due=new_due)
    return f"{success_message} ({affected} row{'s' if affected != 1 else ''})"


async def _run_capture_now(success_message: str) -> str:
    """Queue an immediate capture by flipping the controller flag.

    We don't import the heavy screenshot pipeline from this hot path;
    instead the capture worker watches its controller's ``mark_capture``
    counter and a separate ``force_capture`` flag (best-effort —
    falls back to logging-only when the controller doesn't expose it).
    """
    try:
        from app.workers.control import get_controller  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — controller always ships
        log.warning("quick_actions.capture_now.no_controller", error=str(exc))
        raise HTTPException(
            status_code=503,
            detail="capture controller unavailable",
        ) from exc
    controller = get_controller()
    # ``mark_capture`` is the documented way to nudge the loop; it's an
    # in-memory counter so the next loop tick takes a shot. We don't
    # call the full ``/api/capture/now`` pipeline because that would
    # duplicate the screenshot insert below and turn this fast path
    # into an N+1 storm.
    controller.mark_capture()
    log.info("quick_actions.capture_now.signalled")
    return success_message


async def _run_mic_toggle(success_message: str) -> str:
    """Flip the live mic kill-switch kv flag."""
    async with get_connection() as conn:
        current = await get_kv(conn, _KV_MIC_PAUSED)
        was_paused = (current or "0").strip() == "1"
        next_value = "0" if was_paused else "1"
        await set_kv(conn, _KV_MIC_PAUSED, next_value)
    log.info(
        "quick_actions.mic_toggle",
        was_paused=was_paused,
        now_paused=not was_paused,
    )
    state = "paused" if not was_paused else "live"
    return f"{success_message} (now {state})"


async def _run_install_pair_coding(success_message: str) -> str:
    """Install the Pair Coding focus preset (idempotent)."""
    try:
        profile_id = await install_preset(_PAIR_CODING_PRESET)
    except ValueError as exc:
        # Preset registry drifted out from under us — loud 400 so the
        # operator notices rather than silently succeeding.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("quick_actions.install_pair_coding", profile_id=profile_id)
    return f"{success_message} (id={profile_id})"


__all__ = ["router"]
