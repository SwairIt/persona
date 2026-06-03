/*
 * quick_pin.js — v0.99 feature 2/3
 *
 * Keyboard-driven pin toggle. Press `P` (when no input/textarea/
 * contenteditable is focused) to flip the pinned state on the shot
 * the operator is "looking at":
 *
 *   1. If <body data-shot-id-active> is set (e.g. /screenshot/{id}),
 *      that wins — single-shot pages.
 *   2. Otherwise the currently-focused element (or one of its
 *      ancestors) carrying [data-shot-id] is used — thumbnail grids
 *      that move focus around with Tab / arrow keys.
 *   3. Nothing focused and no body attribute → silent no-op so we
 *      never POST against a guessed id.
 *
 * Endpoint is the spec'd /pin/{id}; if a /pin/{id}/toggle route is
 * available it'll be tried first and falls back to /pin/{id} on 404.
 * Both shapes only need an id in the path, so we send no body and
 * skip CSRF: any state-changing route the server actually wires up
 * is expected to accept a same-origin POST.
 *
 * On success we flash the relevant tile (or the body, on the single-
 * shot page) with a brief accent ring so the operator gets visual
 * confirmation without a full page reload. A console line records
 * the action for debugging; failures log a warning instead of
 * surfacing a modal — the next manual click on the existing Pin
 * button will give a real error path if something is truly broken.
 */
(function () {
  'use strict';

  const FLASH_CLASS = 'quick-pin-flash';
  const FLASH_MS = 600;

  // v1.2 — user-customisable binding. Defaults to "p" (single-key, no
  // modifier) to match the original v0.99 behaviour. Loaded once on
  // init from /api/kbd-shortcuts.json; a failed fetch leaves the
  // binding at its default so the listener keeps working offline.
  const DEFAULT_PIN_KEY = 'p';
  let pinKey = DEFAULT_PIN_KEY;

  // Inject the flash style once. Kept inline so quick_pin.js stays a
  // single file the way the rest of /static/*.js modules do.
  function ensureFlashStyle() {
    if (document.getElementById('quick-pin-style')) return;
    const style = document.createElement('style');
    style.id = 'quick-pin-style';
    style.textContent =
      '.' + FLASH_CLASS + ' {' +
        ' outline: 2px solid #a78bfa;' +
        ' outline-offset: 2px;' +
        ' transition: outline-color ' + FLASH_MS + 'ms ease-out;' +
      '}';
    document.head.appendChild(style);
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (el.isContentEditable) return true;
    return false;
  }

  function findShotId() {
    // 1. Page-level active shot wins (e.g. /screenshot/{id}).
    const bodyId = document.body && document.body.getAttribute('data-shot-id-active');
    if (bodyId) {
      return { id: bodyId, target: document.body };
    }
    // 2. Look up the focus chain for the nearest [data-shot-id].
    const active = document.activeElement;
    if (active && active !== document.body) {
      const host = active.closest && active.closest('[data-shot-id]');
      if (host) {
        const sid = host.getAttribute('data-shot-id');
        if (sid) return { id: sid, target: host };
      }
    }
    return null;
  }

  function flash(target) {
    if (!target || !target.classList) return;
    target.classList.add(FLASH_CLASS);
    setTimeout(() => {
      target.classList.remove(FLASH_CLASS);
    }, FLASH_MS);
  }

  async function postPin(id) {
    // Try the toggle variant first; if the server doesn't wire it up
    // we fall back to the plain /pin/{id} endpoint. Network errors
    // bubble up so the caller can log them.
    const toggleUrl = '/pin/' + encodeURIComponent(id) + '/toggle';
    const plainUrl = '/pin/' + encodeURIComponent(id);
    let res = await fetch(toggleUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
    if (res.status === 404 || res.status === 405) {
      res = await fetch(plainUrl, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' },
      });
    }
    return res;
  }

  async function handleQuickPin() {
    const found = findShotId();
    if (!found) {
      console.debug('[quick_pin] P pressed but no active shot found');
      return;
    }
    try {
      const res = await postPin(found.id);
      if (res.ok) {
        flash(found.target);
        let detail = '';
        try {
          const j = await res.clone().json();
          if (j && (j.tier || j.pinned !== undefined)) {
            detail = ' → ' + (j.tier || (j.pinned ? 'pinned' : 'unpinned'));
          }
        } catch (_) { /* not JSON, that's fine */ }
        console.info('[quick_pin] toggled shot #' + found.id + detail);
      } else {
        console.warn('[quick_pin] toggle failed: HTTP ' + res.status + ' for shot #' + found.id);
      }
    } catch (err) {
      console.warn('[quick_pin] network error toggling shot #' + found.id, err);
    }
  }

  function onKeydown(e) {
    // Ignore modified presses so we don't hijack browser shortcuts
    // (Ctrl+P print, Cmd+P, etc.).
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    // Match either case of the bound single-key letter. We don't try to
    // canonicalise multi-token sequences here — quick-pin is an instant
    // shortcut by design; if the user binds something like "g p" it
    // simply won't fire from this listener and the legacy "g" sequence
    // in keyboard_shortcuts.js (which is unrelated) keeps its meaning.
    const bound = (pinKey || DEFAULT_PIN_KEY).trim();
    if (bound.indexOf(' ') !== -1) return;
    if (e.key !== bound && e.key.toLowerCase() !== bound.toLowerCase()) return;
    if (isTypingTarget(e.target)) return;
    // Also bail if any contenteditable host is the active element —
    // covers the OCR inline editor where the actual <pre> takes focus.
    if (isTypingTarget(document.activeElement)) return;
    e.preventDefault();
    handleQuickPin();
  }

  function loadBinding() {
    // Best-effort fetch — a failure leaves pinKey at its default so the
    // listener still fires on "p" / "P" exactly like the v0.99 behaviour.
    return fetch('/api/kbd-shortcuts.json', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (json) {
        if (!json || typeof json !== 'object') return;
        const value = json.pin_toggle;
        if (typeof value === 'string' && value.trim()) {
          pinKey = value.trim();
        }
      })
      .catch(function () { /* keep default */ });
  }

  function init() {
    ensureFlashStyle();
    document.addEventListener('keydown', onKeydown);
    loadBinding();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
