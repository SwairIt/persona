/*
 * shortcuts_overlay.js
 *
 * Press `?` (Shift+/) from anywhere — except inside an input/textarea/
 * contentEditable element — to open a Spotlight-style cheatsheet of every
 * keyboard shortcut. The list comes from /api/help/shortcuts.json so the
 * route module stays the single source of truth (see
 * app/web/routes/shortcuts_help.py).
 *
 * Behaviour
 * ---------
 *   - First `?` press fetches the JSON; the response is cached in a
 *     module-level variable so subsequent opens are zero-network.
 *   - The overlay is built with inline styles (no separate stylesheet)
 *     so this file is self-contained — drop it on any page that extends
 *     base.html and `?` works without any further wiring.
 *   - Esc, click on backdrop, or pressing `?` again closes the overlay.
 *   - We skip the listener if any other modal-ish element is open
 *     (anything with the `[data-modal-open]` attribute, or a visible
 *     <dialog>). This keeps us from stacking on top of the command
 *     palette / quick-pin / fullscreen viewer.
 *   - All DOM strings are built via document.createElement + .textContent
 *     so we never inject HTML from the JSON payload — XSS-safe even if
 *     a future row contains `<script>`.
 *
 * Failure modes are intentionally quiet — a 404 / offline fetch logs to
 * the console and the keypress becomes a no-op rather than a broken UI.
 */

(function () {
  'use strict';

  var OVERLAY_ID = 'persona-shortcuts-overlay';
  var ENDPOINT = '/api/help/shortcuts.json';

  /** Cached JSON payload after the first successful fetch. */
  var cachedShortcuts = null;
  /** True while an in-flight fetch is pending (avoids double-fetching on a fast double-`?`). */
  var fetchInFlight = false;

  /**
   * Is the active focus target somewhere the user is typing? We must
   * never swallow `?` when the user is composing a search query or note.
   */
  function isTypingTarget(target) {
    if (!target || target.nodeType !== 1) return false;
    var tag = target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (target.isContentEditable) return true;
    return false;
  }

  /**
   * Detect whether another modal-like surface is already on screen, so
   * `?` doesn't pop a cheatsheet on top of the command palette.
   */
  function anotherModalOpen() {
    if (document.querySelector('[data-modal-open]')) return true;
    var dialogs = document.querySelectorAll('dialog[open]');
    if (dialogs.length > 0) return true;
    // Common Alpine pattern in this codebase: a top-level overlay with
    // `x-show` + fixed inset-0 + z-50. We check for a generic visible
    // backdrop sibling so the command palette / drawer don't get
    // covered by the cheatsheet.
    var paletteRoot = document.getElementById('palette-root');
    if (paletteRoot && paletteRoot.childElementCount > 0) {
      // Palette is rendered into a portal only while open.
      return true;
    }
    return false;
  }

  function overlayElement() {
    return document.getElementById(OVERLAY_ID);
  }

  function closeOverlay() {
    var el = overlayElement();
    if (el && el.parentNode) {
      el.parentNode.removeChild(el);
    }
    document.removeEventListener('keydown', onOverlayKey, true);
  }

  function onOverlayKey(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeOverlay();
    }
  }

  /**
   * Build the overlay DOM from a payload array. No HTML injection — every
   * string from the JSON goes through .textContent.
   */
  function buildOverlay(payload) {
    var backdrop = document.createElement('div');
    backdrop.id = OVERLAY_ID;
    backdrop.setAttribute('role', 'dialog');
    backdrop.setAttribute('aria-modal', 'true');
    backdrop.setAttribute('aria-label', 'Keyboard shortcuts');
    backdrop.tabIndex = -1;
    backdrop.style.cssText = [
      'position:fixed',
      'inset:0',
      'background:rgba(0,0,0,0.65)',
      'display:flex',
      'align-items:flex-start',
      'justify-content:center',
      'padding:8vh 16px 16px',
      'z-index:9999',
      'backdrop-filter:blur(4px)',
    ].join(';');

    var card = document.createElement('div');
    card.style.cssText = [
      'background:#1a1a1f',
      'color:#f4f4f5',
      'border:1px solid #26262e',
      'border-radius:12px',
      'max-width:600px',
      'width:100%',
      'max-height:80vh',
      'overflow-y:auto',
      'padding:24px',
      'box-shadow:0 25px 50px -12px rgba(0,0,0,0.6)',
      'font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
    ].join(';');

    var headerRow = document.createElement('div');
    headerRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;';

    var title = document.createElement('h2');
    title.textContent = 'Горячие клавиши';
    title.style.cssText = 'font-size:18px;font-weight:700;margin:0;';
    headerRow.appendChild(title);

    var hint = document.createElement('span');
    hint.textContent = 'Esc — закрыть';
    hint.style.cssText = 'font-size:11px;color:#71717a;';
    headerRow.appendChild(hint);

    card.appendChild(headerRow);

    var grouped = {};
    var order = [];
    for (var i = 0; i < payload.length; i++) {
      var row = payload[i];
      if (!row || typeof row.category !== 'string') continue;
      if (!grouped[row.category]) {
        grouped[row.category] = [];
        order.push(row.category);
      }
      grouped[row.category].push(row);
    }

    for (var j = 0; j < order.length; j++) {
      var cat = order[j];
      var section = document.createElement('section');
      section.style.cssText = 'margin-bottom:16px;';

      var h3 = document.createElement('h3');
      h3.textContent = cat;
      h3.style.cssText = [
        'font-size:11px',
        'text-transform:uppercase',
        'letter-spacing:0.1em',
        'color:#71717a',
        'border-bottom:1px solid #26262e',
        'padding-bottom:6px',
        'margin:0 0 8px 0',
      ].join(';');
      section.appendChild(h3);

      var rows = grouped[cat];
      for (var k = 0; k < rows.length; k++) {
        var item = rows[k];
        var line = document.createElement('div');
        line.style.cssText = 'display:flex;align-items:center;gap:12px;padding:4px 0;';

        var kbd = document.createElement('kbd');
        kbd.textContent = item.key_combo || '';
        kbd.style.cssText = [
          'flex:0 0 96px',
          'padding:4px 8px',
          'background:#26262e',
          'border-radius:4px',
          'font-family:JetBrains Mono,Cascadia Code,Consolas,monospace',
          'font-size:12px',
          'text-align:center',
          'box-shadow:0 1px 2px rgba(0,0,0,0.3)',
        ].join(';');
        line.appendChild(kbd);

        var desc = document.createElement('span');
        desc.textContent = item.description_ru || item.description_en || '';
        desc.style.cssText = 'font-size:13px;color:#e4e4e7;';
        line.appendChild(desc);

        section.appendChild(line);
      }

      card.appendChild(section);
    }

    backdrop.appendChild(card);

    // Click on the backdrop (but not inside the card) closes.
    backdrop.addEventListener('click', function (event) {
      if (event.target === backdrop) {
        closeOverlay();
      }
    });

    return backdrop;
  }

  function openOverlayWith(payload) {
    if (overlayElement()) return; // already open
    var node = buildOverlay(payload);
    document.body.appendChild(node);
    node.focus();
    document.addEventListener('keydown', onOverlayKey, true);
  }

  /**
   * Lazy-load the JSON, then open the overlay. Subsequent calls reuse
   * the cached payload so the second `?` press is instant.
   */
  function showOverlay() {
    if (cachedShortcuts) {
      openOverlayWith(cachedShortcuts);
      return;
    }
    if (fetchInFlight) return;
    fetchInFlight = true;
    fetch(ENDPOINT, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (resp) {
        if (!resp.ok) throw new Error('shortcuts fetch failed: ' + resp.status);
        return resp.json();
      })
      .then(function (json) {
        if (!Array.isArray(json)) throw new Error('shortcuts payload not an array');
        cachedShortcuts = json;
        openOverlayWith(json);
      })
      .catch(function (err) {
        // Quiet failure — log + no-op so a broken endpoint never wedges
        // the page.
        if (window.console && window.console.warn) {
          window.console.warn('Persona shortcuts overlay:', err);
        }
      })
      .then(function () {
        fetchInFlight = false;
      });
  }

  function onGlobalKey(event) {
    // `?` is Shift+/ on US layouts; event.key normalises this across
    // layouts so we don't have to check shiftKey + code separately.
    if (event.key !== '?') return;
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (isTypingTarget(event.target)) return;
    if (overlayElement()) {
      // Second `?` press closes the open overlay (toggle).
      event.preventDefault();
      closeOverlay();
      return;
    }
    if (anotherModalOpen()) return;
    event.preventDefault();
    showOverlay();
  }

  function boot() {
    document.addEventListener('keydown', onGlobalKey);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
