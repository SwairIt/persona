-- v1.45 — Configurable web-side hotkey bindings.
--
-- v1.22 introduced the keyboard cheatsheet overlay (``?``) but every
-- key handler that powers it — capture pause/resume, mic toggle,
-- "capture now", theme toggle, command palette, search focus,
-- quick-pin — is still hard-coded across ``static/keyboard_shortcuts.js``
-- and ``static/quick_pin.js``. Users have been asking for per-action
-- rebinding (some prefer ``P`` for pause, others want ``M`` for mute,
-- a few want ``Cmd+.`` for the mic).
--
-- This table is the authoritative store for those bindings. Unlike the
-- earlier ``kbd_shortcuts_json`` kv blob (migration 083), every action
-- is a first-class row so we can:
--   * disable a binding without losing its custom key combo (``enabled = 0``);
--   * audit per-action edits via the ``created_at`` timestamp;
--   * cheaply enforce uniqueness on ``action`` so a malformed write
--     can't silently produce two rows for the same action.
--
-- Schema notes:
--   hotkey_binding.action      — stable machine-friendly key
--                                (``capture_pause``, ``mic_toggle`` …);
--                                UNIQUE so two rows for the same action
--                                are a write-time error, not a render-
--                                time tiebreaker.
--   hotkey_binding.key_combo   — exactly the string the front-end
--                                compares against ``event.key``
--                                (single letter ``"P"`` / ``"M"``, the
--                                special tokens ``"Question"`` and
--                                ``"Slash"``, or a modifier combo like
--                                ``"Cmd+K"`` / ``"Shift+P"``). The JS
--                                layer canonicalises before compare so
--                                case is irrelevant for plain letters.
--   hotkey_binding.enabled     — soft disable. Lets the operator turn
--                                off a single binding (e.g. ``P`` for
--                                capture_pause when they keep mashing
--                                it during typing in apps without a
--                                proper focus shield) without erasing
--                                the customised key.
--   hotkey_binding.created_at  — first-write timestamp; we never UPDATE
--                                it on edit so it doubles as a "when
--                                did this user first customise this
--                                action" marker for analytics.

CREATE TABLE IF NOT EXISTS hotkey_binding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL UNIQUE,
    key_combo TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Seed the eight default bindings. ``INSERT OR IGNORE`` keeps the
-- migration idempotent — re-running it after the user has customised
-- their bindings will never overwrite a row, because the UNIQUE
-- constraint on ``action`` causes the insert to be ignored.
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('capture_pause', 'P');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('capture_now', 'C');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('mic_toggle', 'M');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('theme_toggle', 'T');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('command_palette', 'Cmd+K');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('shortcuts_help', 'Question');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('search_focus', 'Slash');
INSERT OR IGNORE INTO hotkey_binding (action, key_combo) VALUES ('quick_pin', 'Shift+P');
