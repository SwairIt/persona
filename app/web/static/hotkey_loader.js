/*
 * Persona — configurable hotkey loader (v1.45).
 *
 * Vanilla ES2020. No frameworks.
 *
 * On DOMContentLoaded:
 *   1. Fetches /api/hotkeys.json.
 *   2. For each enabled action, registers a global keydown listener
 *      that matches the user's key_combo against the event and, on a
 *      hit, dispatches to window.PersonaHotkeys.handlers[action].
 *
 * Other JS files register their handlers on the
 * ``window.PersonaHotkeys.handlers`` dict:
 *
 *   window.PersonaHotkeys = window.PersonaHotkeys || {};
 *   window.PersonaHotkeys.handlers = window.PersonaHotkeys.handlers || {};
 *   window.PersonaHotkeys.handlers.capture_pause = function (ev) { ... };
 *
 * An action with no registered handler is a silent no-op — the listener
 * fires but does nothing. This lets the catalogue grow ahead of the JS
 * wiring without breaking the page.
 *
 * Key-combo grammar (case-insensitive on the letter part):
 *   "P"                — single letter, no modifier.
 *   "Cmd+K" / "Ctrl+K" — letter with Cmd (macOS) or Ctrl (others).
 *                        "Cmd" matches event.metaKey on Mac and
 *                        event.ctrlKey on Win/Linux; "Ctrl" always
 *                        matches event.ctrlKey.
 *   "Shift+P"          — letter with Shift held.
 *   "Cmd+."            — punctuation with a modifier.
 *   "Question"         — the literal "?" key (matches event.key === "?").
 *   "Slash"            — the literal "/" key (matches event.key === "/").
 *
 * The matcher never hijacks keystrokes aimed at <input>, <textarea>,
 * <select>, or contenteditable elements unless the combo carries an
 * explicit modifier — Cmd+K still opens the palette while typing in
 * the search box, but pressing P doesn't fire capture_pause while you
 * are writing a note.
 */
(function () {
  'use strict';

  // Bootstrap the registry namespace so handler-side scripts can do
  // ``window.PersonaHotkeys.handlers.foo = ...`` regardless of script
  // load order. We deliberately do NOT clobber an existing object.
  window.PersonaHotkeys = window.PersonaHotkeys || {};
  window.PersonaHotkeys.handlers = window.PersonaHotkeys.handlers || {};
  /** @type {Record<string, {key_combo: string, enabled: boolean}>} */
  window.PersonaHotkeys.bindings = window.PersonaHotkeys.bindings || {};

  function isTyping(target) {
    if (!target || target.nodeType !== 1) return false;
    const el = /** @type {HTMLElement} */ (target);
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function isMac() {
    return /Mac|iPhone|iPad|iPod/.test(window.navigator.platform || '');
  }

  /**
   * Parse a key_combo string into a normalised match descriptor.
   * Unknown / malformed combos return null so the caller can skip the
   * binding instead of throwing during keydown processing.
   *
   * @param {string} combo
   * @returns {{key: string, ctrl: boolean, shift: boolean, alt: boolean, meta: boolean, useCode: boolean} | null}
   */
  function parseCombo(combo) {
    if (typeof combo !== 'string') return null;
    const parts = combo.split('+').map(function (p) { return p.trim(); }).filter(Boolean);
    if (parts.length === 0) return null;
    let ctrl = false, shift = false, alt = false, meta = false;
    /** @type {string | null} */
    let key = null;
    for (let i = 0; i < parts.length; i++) {
      const tok = parts[i];
      const lower = tok.toLowerCase();
      if (lower === 'cmd' || lower === 'meta' || lower === 'super') {
        if (isMac()) meta = true; else ctrl = true;
      } else if (lower === 'ctrl' || lower === 'control') {
        ctrl = true;
      } else if (lower === 'shift') {
        shift = true;
      } else if (lower === 'alt' || lower === 'option') {
        alt = true;
      } else {
        // The non-modifier part. Last one wins so "Cmd+Shift+P" parses
        // with key="P" rather than the meaningless "Shift".
        key = tok;
      }
    }
    if (key === null) return null;
    // Question / Slash are special: we compare against event.key for
    // "?" and "/" so the binding survives non-QWERTY keyboard layouts
    // that put them on different physical keys.
    const useCode = false;
    return { key: key, ctrl: ctrl, shift: shift, alt: alt, meta: meta, useCode: useCode };
  }

  /**
   * Resolve the literal string we compare against ``event.key`` for a
   * given parsed combo's ``key`` field.
   */
  function expectedKey(parsedKey) {
    const lower = parsedKey.toLowerCase();
    if (lower === 'question') return '?';
    if (lower === 'slash') return '/';
    if (lower === 'period' || lower === 'dot') return '.';
    if (lower === 'comma') return ',';
    if (lower === 'space') return ' ';
    if (lower === 'escape' || lower === 'esc') return 'Escape';
    if (lower === 'enter' || lower === 'return') return 'Enter';
    if (lower === 'tab') return 'Tab';
    return parsedKey;
  }

  /**
   * Does the keyboard event match the parsed combo? Letters compare
   * case-insensitively; punctuation compares literally so "Cmd+." is
   * distinct from "Cmd+>".
   */
  function eventMatches(ev, parsed) {
    if (parsed.ctrl !== ev.ctrlKey) return false;
    if (parsed.alt !== ev.altKey) return false;
    if (parsed.meta !== ev.metaKey) return false;
    // Shift is fiddly: when the binding is a plain letter we don't
    // require Shift even though the underlying KeyboardEvent will
    // report shiftKey=true for an uppercase letter on a QWERTY
    // keyboard. For combos that explicitly carry "Shift+" in the
    // string we DO require it.
    if (parsed.shift && !ev.shiftKey) return false;
    const expect = expectedKey(parsed.key);
    if (expect.length === 1) {
      // Single-character target: compare case-insensitively.
      return String(ev.key).toLowerCase() === expect.toLowerCase();
    }
    // Multi-character name (Escape / Enter / etc.) — exact compare.
    return ev.key === expect;
  }

  /**
   * Build the global keydown listener from the current bindings map.
   * Re-callable so the page can refresh bindings after the user edits
   * a row in /settings/hotkeys without a full reload.
   */
  function installListener(bindings) {
    // Compile parse results once so we don't re-parse on every key.
    /** @type {Array<{action: string, parsed: ReturnType<typeof parseCombo>}>} */
    const compiled = [];
    for (const action of Object.keys(bindings)) {
      const row = bindings[action];
      if (!row || row.enabled === false) continue;
      const parsed = parseCombo(row.key_combo);
      if (!parsed) continue;
      compiled.push({ action: action, parsed: parsed });
    }

    function onKeyDown(ev) {
      for (let i = 0; i < compiled.length; i++) {
        const item = compiled[i];
        const parsed = item.parsed;
        if (!parsed) continue;
        // Plain letter bindings (no modifier) must not fire while the
        // user is typing into a field. Modifier-bearing combos (Cmd+K,
        // Shift+P, Cmd+.) are still allowed because they don't collide
        // with normal typing.
        const hasModifier = parsed.ctrl || parsed.alt || parsed.meta || parsed.shift;
        if (!hasModifier && isTyping(ev.target)) continue;
        if (!eventMatches(ev, parsed)) continue;
        const handler = window.PersonaHotkeys.handlers[item.action];
        if (typeof handler !== 'function') {
          // No registered handler — silently skip. The catalogue is
          // allowed to grow ahead of the JS wiring.
          continue;
        }
        try {
          ev.preventDefault();
          handler(ev);
        } catch (err) {
          // A buggy handler should not nuke the whole listener for
          // other actions; log and continue.
          console.warn('[hotkey_loader] handler for', item.action, 'threw:', err);
        }
        return;
      }
    }

    // Replace any previous listener so re-installing after a save
    // doesn't stack duplicates.
    if (window.PersonaHotkeys.__currentListener) {
      document.removeEventListener('keydown', window.PersonaHotkeys.__currentListener);
    }
    document.addEventListener('keydown', onKeyDown);
    window.PersonaHotkeys.__currentListener = onKeyDown;
  }

  async function loadBindings() {
    try {
      const res = await fetch('/api/hotkeys.json', {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      });
      if (!res.ok) return;
      const json = await res.json();
      if (!json || typeof json !== 'object') return;
      window.PersonaHotkeys.bindings = json;
      installListener(json);
    } catch (err) {
      // Network / parse failure: leave the page running without
      // configurable hotkeys. The legacy hard-coded handlers in
      // keyboard_shortcuts.js / quick_pin.js keep working.
      console.warn('[hotkey_loader] failed to load /api/hotkeys.json:', err);
    }
  }

  // Expose a small helper so page scripts can force a refresh after
  // editing a binding from /settings/hotkeys.
  window.PersonaHotkeys.refresh = loadBindings;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadBindings);
  } else {
    loadBindings();
  }
})();
