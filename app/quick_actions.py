"""Quick-actions catalogue.

A single source of truth for the one-click panel exposed by
:mod:`app.web.routes.quick_actions`. Every entry describes one action
the operator can fire from the timeline or any HTMX-embedded fragment:

- ``action_id``        — stable slug used in the run-endpoint URL.
- ``label``            — short button text.
- ``description``      — one-sentence tooltip / aria-label.
- ``method``           — ``"POST"`` or ``"GET"``; informational only,
                         the run-endpoint always handles the action via
                         a direct DB call rather than re-issuing HTTP.
- ``url``              — canonical HTTP endpoint the action *would*
                         hit if the panel were a dumb form-poster. Kept
                         for documentation / discoverability of the
                         feature surface; the run-endpoint never
                         actually fetches it.
- ``payload``          — example JSON body the canonical endpoint
                         expects, or ``None`` for endpoints that take
                         no body. ``"{input}"`` is a literal marker
                         that the run-endpoint should substitute the
                         user-supplied ``input`` string into.
- ``icon_glyph``       — single unicode glyph for the button (rendered
                         inline; no icon font required).
- ``success_message``  — copy returned in the JSON response after a
                         successful run.

All SQL invoked by :mod:`app.web.routes.quick_actions` is parametrised
— this module only declares metadata and runs no queries itself.

Some catalogue entries point at endpoints that are not yet implemented
in this repo at the time this module was written. They are kept in the
catalogue so the panel surface stays stable, and the run-endpoint is
expected to short-circuit unknown actions with a 501.

External dependencies (must exist for the action to *run*):

- ``add_note``                   — ``app.storage.notes.insert_inbox_note``
- ``pin_last_shot``              — table ``screenshots`` with column
                                   ``pinned_at`` (see
                                   :mod:`app.auto_pin_engine`).
- ``snooze_all_reminders``       — table ``ai_reminder`` with column
                                   ``due_at`` (see
                                   :mod:`app.web.routes.ai_reminders`).
- ``capture_now``                — ``POST /api/capture/now`` exists
                                   (:mod:`app.web.routes.capture_api`);
                                   triggered via direct controller call.
- ``mic_toggle``                 — ``POST /api/audio/mic`` exists
                                   (:mod:`app.web.routes.mic_toggle`);
                                   triggered via direct kv flip.
- ``new_focus_pair_coding``      — preset row ``"Pair Coding"`` in
                                   :data:`app.focus_profiles.PRESET_PROFILES`.
"""

from __future__ import annotations

from typing import Final, TypedDict

from app.logging_setup import get_logger

log = get_logger("persona.quick_actions")


class QuickAction(TypedDict):
    """One entry in the quick-actions catalogue."""

    action_id: str
    label: str
    description: str
    method: str
    url: str
    payload: dict[str, object] | None
    icon_glyph: str
    success_message: str


#: Catalogue of all quick actions. Order matters — the panel renders
#: buttons in this exact order so the most common action sits top-left.
ACTIONS: Final[list[QuickAction]] = [
    {
        "action_id": "add_note",
        "label": "Add note",
        "description": "Drop a short markdown note straight into the inbox.",
        "method": "POST",
        "url": "/api/notes",
        "payload": {"body": "{input}"},
        "icon_glyph": "+",
        "success_message": "Note saved to inbox.",
    },
    {
        "action_id": "pin_last_shot",
        "label": "Pin last shot",
        "description": "Mark the most recent screenshot as pinned.",
        "method": "POST",
        "url": "/api/screenshot/{latest_id}/pin",
        "payload": None,
        "icon_glyph": "★",
        "success_message": "Latest screenshot pinned.",
    },
    {
        "action_id": "snooze_all_reminders",
        "label": "Snooze reminders 4h",
        "description": "Push every undismissed AI reminder 4 hours into the future.",
        "method": "POST",
        "url": "/api/ai-reminders/snooze-all",
        "payload": {"hours": 4},
        "icon_glyph": "⏰",
        "success_message": "All open reminders snoozed by 4h.",
    },
    {
        "action_id": "capture_now",
        "label": "Capture now",
        "description": "Force a single screenshot immediately.",
        "method": "POST",
        "url": "/api/capture/now",
        "payload": None,
        "icon_glyph": "●",
        "success_message": "Capture queued.",
    },
    {
        "action_id": "mic_toggle",
        "label": "Toggle mic",
        "description": "Flip the live microphone kill-switch.",
        "method": "POST",
        "url": "/api/audio/mic/toggle",
        "payload": None,
        "icon_glyph": "🎙",
        "success_message": "Microphone state flipped.",
    },
    {
        "action_id": "new_focus_pair_coding",
        "label": "Install Pair Coding",
        "description": "Install the built-in Pair Coding focus profile preset.",
        "method": "POST",
        "url": "/focus/profiles/install-preset/Pair Coding",
        "payload": None,
        "icon_glyph": "👥",
        "success_message": "Pair Coding profile installed.",
    },
]


def find_action(action_id: str) -> QuickAction | None:
    """Return the catalogue entry for ``action_id`` or ``None``.

    The lookup is exact-match; unknown ids surface as ``None`` so the
    caller can return a 404 rather than silently no-op.
    """
    for action in ACTIONS:
        if action["action_id"] == action_id:
            return action
    return None


__all__ = ["ACTIONS", "QuickAction", "find_action"]
