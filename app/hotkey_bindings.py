"""Configurable web-side hotkey bindings (v1.45).

The v1.22 cheatsheet overlay (``?``) and the v0.99 quick-pin shortcut
both hard-coded their key handlers in ``static/keyboard_shortcuts.js``
and ``static/quick_pin.js``. Users want to rebind those — some prefer
``P`` for capture pause, others want ``M`` for the mic toggle, a few
want ``Cmd+.`` for mic. This module is the storage-layer service for
that rebinding.

Design notes
------------
* Store is the dedicated ``hotkey_binding`` table from migration
  ``121_hotkey_bindings.sql``. Producers call :func:`list_bindings`,
  :func:`update_binding`, :func:`reset_to_defaults`.
* The action whitelist is :data:`ACTION_CATALOGUE` — a code-defined
  dict mapping the eight machine-friendly action names to their
  human-readable title + description + canonical default combo. A
  POST against an unknown action key surfaces a tidy
  :class:`ValueError` at the call site instead of silently mis-routing
  to a wrong row.
* ``key_combo`` is intentionally a free-form ``TEXT`` — the front-end
  in :file:`static/hotkey_loader.js` canonicalises before compare, so
  the operator can type ``"p"``, ``"P"``, or even ``"Shift+P"`` and
  the JS layer figures out whether a given ``KeyboardEvent`` matches.
  Validation here is therefore minimal (non-empty, no embedded NULs).
* Every helper opens its own short-lived connection via
  :func:`app.storage.db.get_connection`. The table is tiny
  (one row per action — eight today) so a connection-per-call has no
  measurable cost and keeps the helpers composable.
* Structured logs go through ``persona.hotkey_bindings``.
"""

from __future__ import annotations

from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.hotkey_bindings")


class ActionMeta(TypedDict):
    """Catalogue entry shape — UI metadata + the default combo."""

    title: str
    description: str
    default_combo: str


# The single source of truth for which actions a user is allowed to
# rebind. Adding a new entry here is the only hand-edit needed to
# expose a new action in the settings UI; the runtime listener in
# :file:`static/hotkey_loader.js` looks the key up in
# ``window.PersonaHotkeys.handlers`` and silently skips actions that
# do not have a registered handler — so this catalogue can grow ahead
# of the JS wiring without breaking the page.
#
# The ``default_combo`` strings match the seed values in migration
# ``121_hotkey_bindings.sql``. The two non-letter defaults
# (``"Question"``, ``"Slash"``) match the ``KeyboardEvent.code``
# names that fire for ``?`` and ``/`` regardless of the active keyboard
# layout — the JS layer compares against ``event.code`` for those two
# tokens specifically so a Russian or German layout still triggers
# them on the physical key.
ACTION_CATALOGUE: Final[dict[str, ActionMeta]] = {
    "capture_pause": {
        "title": "Pause / resume capture",
        "description": "Toggle the screen-capture pipeline between paused and running.",
        "default_combo": "P",
    },
    "capture_now": {
        "title": "Capture now",
        "description": "Force-record a single frame regardless of the idle / motion gates.",
        "default_combo": "C",
    },
    "mic_toggle": {
        "title": "Mic toggle",
        "description": "Mute / unmute the microphone without restarting the audio worker.",
        "default_combo": "M",
    },
    "theme_toggle": {
        "title": "Theme toggle",
        "description": "Flip between the dark and light themes.",
        "default_combo": "T",
    },
    "command_palette": {
        "title": "Command palette",
        "description": "Open the Cmd+K command palette overlay.",
        "default_combo": "Cmd+K",
    },
    "shortcuts_help": {
        "title": "Shortcuts cheatsheet",
        "description": "Open the keyboard shortcuts overlay.",
        "default_combo": "Question",
    },
    "search_focus": {
        "title": "Focus search input",
        "description": "Move keyboard focus to the page's search box.",
        "default_combo": "Slash",
    },
    "quick_pin": {
        "title": "Quick pin",
        "description": "Toggle the pinned state on the currently-active shot.",
        "default_combo": "Shift+P",
    },
}
"""Action whitelist + UI metadata + default key combos.

The keys here are the same strings stored in ``hotkey_binding.action``;
the row order is the order the settings page renders the table."""


class BindingRow(TypedDict):
    """Shape returned by :func:`list_bindings` for the UI layer."""

    action: str
    title: str
    description: str
    key_combo: str
    default_combo: str
    enabled: bool


def _validate_action(action: str) -> str:
    """Return ``action`` unchanged if it is a known catalogue key.

    Raises :class:`ValueError` if the action is unknown so a malformed
    POST surfaces at the call site rather than silently no-op-ing
    against a non-existent row.
    """
    if action not in ACTION_CATALOGUE:
        msg = f"unknown hotkey action: {action!r}"
        raise ValueError(msg)
    return action


def _validate_key_combo(key_combo: str) -> str:
    """Trim and sanity-check a user-supplied key combo string.

    We accept anything non-empty without an embedded NUL — the JS
    layer is the final arbiter of whether a combo can be matched
    against a real :class:`KeyboardEvent`. Validation here exists to
    block obvious garbage (empty string, control characters) that
    would render as a broken row in the settings table.
    """
    trimmed = key_combo.strip()
    if not trimmed:
        msg = "key_combo must not be empty"
        raise ValueError(msg)
    if "\x00" in trimmed:
        msg = "key_combo must not contain NUL bytes"
        raise ValueError(msg)
    return trimmed


async def list_bindings() -> list[BindingRow]:
    """Return the current bindings for every action in the catalogue.

    Rows missing from the DB (e.g. a fresh install where the migration
    has not run yet, or an action added to :data:`ACTION_CATALOGUE`
    after the table was seeded) are synthesised from the catalogue
    default so the settings page always renders a complete table.
    Row order matches :data:`ACTION_CATALOGUE` insertion order, which
    is also the seed order in the migration.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT action, key_combo, enabled FROM hotkey_binding"
        )
        rows = await cursor.fetchall()

    stored: dict[str, tuple[str, bool]] = {
        str(row["action"]): (str(row["key_combo"]), bool(row["enabled"]))
        for row in rows
    }
    bindings: list[BindingRow] = []
    for action, meta in ACTION_CATALOGUE.items():
        if action in stored:
            combo, enabled = stored[action]
        else:
            combo, enabled = meta["default_combo"], True
        bindings.append(
            {
                "action": action,
                "title": meta["title"],
                "description": meta["description"],
                "key_combo": combo,
                "default_combo": meta["default_combo"],
                "enabled": enabled,
            }
        )
    return bindings


async def update_binding(action: str, key_combo: str) -> None:
    """Persist a new ``key_combo`` for ``action``.

    Uses an ``INSERT OR REPLACE`` so the call works equally well on a
    row that already exists (the normal case after the migration has
    seeded eight defaults) and on a row that was missing for any
    reason. The ``enabled`` flag is preserved when the row exists by
    explicitly carrying it through the upsert — a plain
    ``INSERT OR REPLACE`` would otherwise reset it to the column
    default of ``1``.
    """
    action = _validate_action(action)
    key_combo = _validate_key_combo(key_combo)
    async with get_connection() as conn:
        # Read the existing ``enabled`` flag so we never accidentally
        # re-enable a binding the operator had soft-disabled.
        cursor = await conn.execute(
            "SELECT enabled FROM hotkey_binding WHERE action = ?",
            (action,),
        )
        existing = await cursor.fetchone()
        enabled = int(existing["enabled"]) if existing is not None else 1
        await conn.execute(
            "INSERT INTO hotkey_binding (action, key_combo, enabled) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(action) DO UPDATE SET key_combo = excluded.key_combo",
            (action, key_combo, enabled),
        )
        await conn.commit()
    log.info(
        "hotkey_bindings.update",
        action=action,
        key_combo=key_combo,
        was_existing=existing is not None,
    )


async def reset_to_defaults() -> int:
    """Restore every binding to its catalogue default and return the row count.

    Wipes the entire ``hotkey_binding`` table then re-seeds it from
    :data:`ACTION_CATALOGUE`. Returns the number of rows written so the
    HTTP route can include it in a flash message ("8 bindings reset").
    """
    async with get_connection() as conn:
        await conn.execute("DELETE FROM hotkey_binding")
        await conn.executemany(
            "INSERT INTO hotkey_binding (action, key_combo) VALUES (?, ?)",
            [(action, meta["default_combo"]) for action, meta in ACTION_CATALOGUE.items()],
        )
        await conn.commit()
    written = len(ACTION_CATALOGUE)
    log.info("hotkey_bindings.reset", written=written)
    return written


__all__ = [
    "ACTION_CATALOGUE",
    "ActionMeta",
    "BindingRow",
    "list_bindings",
    "reset_to_defaults",
    "update_binding",
]
