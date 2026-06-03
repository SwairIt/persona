/*
 * image_viewer.js — Persona v0.46 + v0.71 zoom deep-link
 *
 * Vanilla-JS pinch / wheel zoom + click-drag pan for any element
 * marked with [data-zoomable]. Pure ES2020, no framework.
 *
 * Behaviour:
 *   - mouse wheel  -> zoom around cursor, clamped to [MIN_SCALE, MAX_SCALE]
 *   - pointerdown + drag -> pan
 *   - dblclick -> reset to identity (scale 1, no translation)
 *   - two-finger touch -> pinch-zoom around midpoint; one finger -> pan
 *
 * The viewer mutates `transform` on the zoomable element. The element's
 * parent must clip overflow so panned-out pixels do not leak.
 * `image_viewer.css` ensures that.
 *
 * If no [data-zoomable] elements exist on the page (e.g. the screenshot
 * has no thumbnail), this script is a no-op — no errors, no listeners.
 *
 * v0.71 zoom deep-link:
 *   On page load we sniff ?zoom=&x=&y= from the URL and, if all three
 *   parse as finite numbers with zoom in [MIN_SCALE, MAX_SCALE], apply
 *   them to the *first* zoomable element immediately. This lets links
 *   like /screenshot/42?zoom=3.5&x=-180&y=-220 reopen at a specific
 *   pan/zoom for sharing. The values are the same `tx`/`ty`/`scale`
 *   we already write into the CSS transform — no coordinate mapping.
 *
 *   A companion "Copy zoom link" button (selector [data-copy-zoom-link])
 *   reads the current state from the same zoomable element and copies
 *   `location.pathname?zoom=&x=&y=` to the clipboard. Falls back to a
 *   hidden <textarea> + execCommand when the async Clipboard API is
 *   unavailable (e.g. non-HTTPS contexts). Brief visual feedback is
 *   given by swapping the button label for ~1.2s.
 *
 * v0.95 fullscreen:
 *   Press `F` (when not typing into an input) or click any
 *   `[data-fullscreen]` button to call `requestFullscreen()` on the
 *   *first* zoomable element — the page chrome disappears and the
 *   image fills the screen. `Esc` exits via the browser default; we
 *   also no-op gracefully when the Fullscreen API is unavailable
 *   (older browsers, sandboxed iframes), in which case the button
 *   hides itself so we don't advertise a control that does nothing.
 *   The `F` key is intentionally a global capture so it works while
 *   the cursor is anywhere on the page, not just over the image.
 */
(function () {
  'use strict';

  const MIN_SCALE = 1.0;
  const MAX_SCALE = 8.0;
  const WHEEL_STEP = 0.0015; // delta-per-pixel; tuned for both trackpad + wheel

  /** Clamp `n` into [lo, hi]. */
  function clamp(n, lo, hi) {
    return Math.min(hi, Math.max(lo, n));
  }

  /**
   * Parse a finite number from a URL query string. Returns `null` when the
   * param is missing, empty, or not a finite number. We deliberately do
   * NOT coerce things like "1e9999" or "NaN" — `Number.isFinite` rejects
   * both, which is what we want for an externally-supplied link.
   */
  function parseFiniteParam(params, key) {
    if (!params.has(key)) return null;
    const raw = params.get(key);
    if (raw === '' || raw === null) return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  /**
   * State for one zoomable element. We keep it in a closure rather than on
   * the DOM node so multiple viewers on the same page stay independent.
   */
  function createViewer(el) {
    const state = {
      scale: 1,
      tx: 0,
      ty: 0,
      // pointer-pan bookkeeping
      panning: false,
      panPointerId: null,
      panStartX: 0,
      panStartY: 0,
      panStartTx: 0,
      panStartTy: 0,
      // pinch bookkeeping (touch only)
      pinchActive: false,
      pinchStartDist: 0,
      pinchStartScale: 1,
      pinchAnchorX: 0,
      pinchAnchorY: 0,
      pinchStartTx: 0,
      pinchStartTy: 0,
      // map of active touch pointers -> {x, y}
      touches: new Map(),
    };

    function apply() {
      el.style.transform =
        'translate(' + state.tx + 'px, ' + state.ty + 'px) ' +
        'scale(' + state.scale + ')';
    }

    function reset() {
      state.scale = 1;
      state.tx = 0;
      state.ty = 0;
      apply();
    }

    /**
     * Zoom by multiplier `factor` while keeping the point (anchorX, anchorY)
     * (in client-pixel coordinates relative to the element's bounding box)
     * pinned to the same screen position. This is the standard "zoom toward
     * cursor" trick: translate by the difference in anchor-pixel offset
     * between the old and new scale.
     */
    function zoomAt(factor, anchorX, anchorY) {
      const newScale = clamp(state.scale * factor, MIN_SCALE, MAX_SCALE);
      const effective = newScale / state.scale;
      if (effective === 1) return;

      // The translation update keeps (anchorX, anchorY) fixed:
      //   anchor = tx + scale * local
      //   local = (anchor - tx) / scale
      //   newTx = anchor - newScale * local = anchor - effective * (anchor - tx)
      state.tx = anchorX - effective * (anchorX - state.tx);
      state.ty = anchorY - effective * (anchorY - state.ty);
      state.scale = newScale;

      // If we hit the minimum, recenter so the image snaps back cleanly.
      if (state.scale <= MIN_SCALE + 1e-6) {
        state.tx = 0;
        state.ty = 0;
        state.scale = MIN_SCALE;
      }
      apply();
    }

    function anchorFromEvent(ev) {
      const rect = el.getBoundingClientRect();
      return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
    }

    // --- wheel zoom -------------------------------------------------------
    el.addEventListener('wheel', function (ev) {
      ev.preventDefault();
      const anchor = anchorFromEvent(ev);
      // Negative deltaY = scroll up = zoom in. Exponential keeps the
      // perceived step constant across scale levels.
      const factor = Math.exp(-ev.deltaY * WHEEL_STEP);
      zoomAt(factor, anchor.x, anchor.y);
    }, { passive: false });

    // --- mouse / pen pan via pointer events -------------------------------
    el.addEventListener('pointerdown', function (ev) {
      if (ev.pointerType === 'touch') return; // touch handled separately
      if (ev.button !== 0) return;
      state.panning = true;
      state.panPointerId = ev.pointerId;
      state.panStartX = ev.clientX;
      state.panStartY = ev.clientY;
      state.panStartTx = state.tx;
      state.panStartTy = state.ty;
      el.classList.add('is-dragging');
      try { el.setPointerCapture(ev.pointerId); } catch (_) { /* ignore */ }
    });

    el.addEventListener('pointermove', function (ev) {
      if (!state.panning || ev.pointerId !== state.panPointerId) return;
      state.tx = state.panStartTx + (ev.clientX - state.panStartX);
      state.ty = state.panStartTy + (ev.clientY - state.panStartY);
      apply();
    });

    function endPan(ev) {
      if (!state.panning) return;
      if (ev && ev.pointerId !== state.panPointerId) return;
      state.panning = false;
      state.panPointerId = null;
      el.classList.remove('is-dragging');
      if (ev) {
        try { el.releasePointerCapture(ev.pointerId); } catch (_) { /* ignore */ }
      }
    }
    el.addEventListener('pointerup', endPan);
    el.addEventListener('pointercancel', endPan);
    el.addEventListener('pointerleave', endPan);

    // --- dblclick reset ---------------------------------------------------
    el.addEventListener('dblclick', function (ev) {
      ev.preventDefault();
      reset();
    });

    // --- touch: 1 finger pan, 2 finger pinch -----------------------------
    function touchDist() {
      const pts = Array.from(state.touches.values());
      if (pts.length < 2) return 0;
      const dx = pts[0].x - pts[1].x;
      const dy = pts[0].y - pts[1].y;
      return Math.hypot(dx, dy);
    }

    function touchMid() {
      const pts = Array.from(state.touches.values());
      if (pts.length < 2) return { x: 0, y: 0 };
      const rect = el.getBoundingClientRect();
      return {
        x: (pts[0].x + pts[1].x) / 2 - rect.left,
        y: (pts[0].y + pts[1].y) / 2 - rect.top,
      };
    }

    el.addEventListener('touchstart', function (ev) {
      for (const t of ev.changedTouches) {
        state.touches.set(t.identifier, { x: t.clientX, y: t.clientY });
      }
      if (state.touches.size === 2) {
        // Begin pinch — capture baseline distance and midpoint.
        ev.preventDefault();
        state.pinchActive = true;
        state.pinchStartDist = touchDist() || 1;
        state.pinchStartScale = state.scale;
        const mid = touchMid();
        state.pinchAnchorX = mid.x;
        state.pinchAnchorY = mid.y;
        state.pinchStartTx = state.tx;
        state.pinchStartTy = state.ty;
      } else if (state.touches.size === 1) {
        // Begin one-finger pan (manual — pointer events on touch are skipped
        // because some browsers fire both pointer + touch, doubling motion).
        const only = state.touches.values().next().value;
        state.panning = true;
        state.panPointerId = 'touch';
        state.panStartX = only.x;
        state.panStartY = only.y;
        state.panStartTx = state.tx;
        state.panStartTy = state.ty;
      }
    }, { passive: false });

    el.addEventListener('touchmove', function (ev) {
      for (const t of ev.changedTouches) {
        if (state.touches.has(t.identifier)) {
          state.touches.set(t.identifier, { x: t.clientX, y: t.clientY });
        }
      }
      if (state.pinchActive && state.touches.size >= 2) {
        ev.preventDefault();
        const dist = touchDist();
        if (!dist) return;
        const ratio = dist / state.pinchStartDist;
        const newScale = clamp(state.pinchStartScale * ratio, MIN_SCALE, MAX_SCALE);
        const effective = newScale / state.pinchStartScale;
        // Anchor-preserving translate using the *initial* anchor/translate.
        state.tx = state.pinchAnchorX - effective * (state.pinchAnchorX - state.pinchStartTx);
        state.ty = state.pinchAnchorY - effective * (state.pinchAnchorY - state.pinchStartTy);
        state.scale = newScale;
        if (state.scale <= MIN_SCALE + 1e-6) {
          state.tx = 0;
          state.ty = 0;
          state.scale = MIN_SCALE;
        }
        apply();
      } else if (state.panning && state.panPointerId === 'touch' && state.touches.size === 1) {
        ev.preventDefault();
        const only = state.touches.values().next().value;
        state.tx = state.panStartTx + (only.x - state.panStartX);
        state.ty = state.panStartTy + (only.y - state.panStartY);
        apply();
      }
    }, { passive: false });

    function touchEnd(ev) {
      for (const t of ev.changedTouches) {
        state.touches.delete(t.identifier);
      }
      if (state.touches.size < 2) state.pinchActive = false;
      if (state.touches.size === 0 && state.panPointerId === 'touch') {
        state.panning = false;
        state.panPointerId = null;
      } else if (state.touches.size === 1 && state.panPointerId === 'touch') {
        // One finger lifted from a pinch — restart pan baseline.
        const only = state.touches.values().next().value;
        state.panStartX = only.x;
        state.panStartY = only.y;
        state.panStartTx = state.tx;
        state.panStartTy = state.ty;
      }
    }
    el.addEventListener('touchend', touchEnd);
    el.addEventListener('touchcancel', touchEnd);

    // v0.71 — expose a tiny accessor for the deep-link button. We attach a
    // function (not the raw state) so the closure stays the single source
    // of truth and outside code can't mutate scale/tx/ty by accident.
    // Returns a fresh snapshot each call; values match what's in the CSS
    // transform at the moment the call is made.
    el.__zoomState = function () {
      return { scale: state.scale, tx: state.tx, ty: state.ty };
    };

    // Initial paint so the transform string exists even before interaction.
    apply();
    return { state, apply };
  }

  /**
   * v0.71 — apply ?zoom=&x=&y= from the URL to the first viewer.
   *
   * All three params must be present and finite numbers, with zoom inside
   * [MIN_SCALE, MAX_SCALE], otherwise we leave the viewer at its identity
   * defaults. `x`/`y` are not clamped — they're the same `tx`/`ty` that
   * panning produces, and the natural range depends on image size we
   * don't know here. The CSS overflow-clip on `.zoom-wrapper` keeps even
   * pathological values visually contained.
   */
  function applyDeepLink(viewer) {
    const params = new URLSearchParams(window.location.search);
    const zoom = parseFiniteParam(params, 'zoom');
    const x = parseFiniteParam(params, 'x');
    const y = parseFiniteParam(params, 'y');
    if (zoom === null || x === null || y === null) return;
    if (zoom < MIN_SCALE || zoom > MAX_SCALE) return;
    viewer.state.scale = zoom;
    viewer.state.tx = x;
    viewer.state.ty = y;
    viewer.apply();
  }

  /**
   * v0.71 — write `text` to the system clipboard. Prefers the async
   * Clipboard API; falls back to a hidden <textarea> + execCommand for
   * non-HTTPS pages where navigator.clipboard is undefined. Returns a
   * Promise that resolves true on success, false on any failure. We
   * never throw — the caller just shows or hides a "copied" hint.
   */
  function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(() => true, () => false);
    }
    return new Promise((resolve) => {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '0';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
      document.body.removeChild(ta);
      resolve(ok);
    });
  }

  /**
   * v0.71 — bind every [data-copy-zoom-link] button on the page. The
   * button reads the *first* zoomable element's current state (matching
   * applyDeepLink's "first viewer" semantics) and copies
   * `location.pathname?zoom=&x=&y=`. Query is intentionally rebuilt from
   * scratch so we don't carry over stale ?zoom from the current URL or
   * leak unrelated params. The button label flips for ~1.2s to confirm.
   *
   * If there's no zoomable element on the page, the button hides itself
   * so we don't show a control that does nothing useful.
   */
  function bindCopyButtons() {
    const buttons = document.querySelectorAll('[data-copy-zoom-link]');
    if (!buttons.length) return;
    const target = document.querySelector('[data-zoomable]');
    for (const btn of buttons) {
      if (btn.dataset.copyZoomBound === '1') continue;
      btn.dataset.copyZoomBound = '1';
      if (!target || typeof target.__zoomState !== 'function') {
        btn.hidden = true;
        continue;
      }
      const original = btn.textContent;
      btn.addEventListener('click', async function (ev) {
        ev.preventDefault();
        const s = target.__zoomState();
        // Round to 4 decimals — anything finer is below subpixel anyway
        // and just makes the URL noisy. parseFloat strips trailing zeros.
        const fmt = (n) => parseFloat(n.toFixed(4)).toString();
        const params = new URLSearchParams();
        params.set('zoom', fmt(s.scale));
        params.set('x', fmt(s.tx));
        params.set('y', fmt(s.ty));
        const url = window.location.origin + window.location.pathname + '?' + params.toString();
        const ok = await copyToClipboard(url);
        btn.textContent = ok ? 'Copied!' : 'Copy failed';
        btn.disabled = true;
        setTimeout(() => {
          btn.textContent = original;
          btn.disabled = false;
        }, 1200);
      });
    }
  }

  /**
   * v0.95 — detect Fullscreen API support across vendor prefixes. Returns
   * a tiny adapter exposing `request(el)` and `isSupported()`. We probe
   * the element rather than `document` because Safari historically only
   * exposed `webkitRequestFullscreen` on elements. Returns `null` when
   * no implementation is found at all.
   */
  function fullscreenAdapter(el) {
    const fn =
      el.requestFullscreen ||
      el.webkitRequestFullscreen ||
      el.mozRequestFullScreen ||
      el.msRequestFullscreen;
    if (typeof fn !== 'function') return null;
    return {
      request() {
        try {
          const result = fn.call(el);
          // Standard returns a Promise; the prefixed variants don't.
          // Swallow rejections (e.g. "must be called from user gesture"
          // raced with a stale click) so a failure doesn't bubble to
          // the global unhandled-rejection handler.
          if (result && typeof result.catch === 'function') {
            result.catch(() => { /* silently ignore */ });
          }
        } catch (_) {
          /* legacy synchronous throw — also ignore */
        }
      },
    };
  }

  /**
   * v0.95 — bind every [data-fullscreen] button + the global `F` key to
   * fullscreen the first zoomable element. The button hides itself when
   * either there's no zoomable image on the page or the Fullscreen API
   * isn't available; the keyboard shortcut becomes a no-op in the same
   * conditions. The `F` listener guards against typing contexts so it
   * doesn't hijack the letter in textareas / inputs / contenteditable.
   */
  function bindFullscreen() {
    const target = document.querySelector('[data-zoomable]');
    const adapter = target ? fullscreenAdapter(target) : null;
    const enabled = !!adapter;

    const buttons = document.querySelectorAll('[data-fullscreen]');
    for (const btn of buttons) {
      if (btn.dataset.fullscreenBound === '1') continue;
      btn.dataset.fullscreenBound = '1';
      if (!enabled) {
        btn.hidden = true;
        continue;
      }
      btn.addEventListener('click', function (ev) {
        ev.preventDefault();
        adapter.request();
      });
    }

    // Bind the global key once per page even if init() runs again
    // (e.g. via SPA-style re-entry). The flag lives on <html> because
    // `document` itself doesn't carry a dataset.
    if (document.documentElement.dataset.fullscreenKeyBound === '1') return;
    document.documentElement.dataset.fullscreenKeyBound = '1';
    if (!enabled) return;
    document.addEventListener('keydown', function (ev) {
      if (ev.key !== 'f' && ev.key !== 'F') return;
      // Don't hijack the letter in any text-entry context.
      const t = ev.target;
      if (t && (
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) ||
        t.isContentEditable
      )) return;
      // Ignore modifier combos so Ctrl+F (find) and Cmd+F still work.
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
      ev.preventDefault();
      adapter.request();
    });
  }

  function init() {
    const nodes = document.querySelectorAll('[data-zoomable]');
    if (!nodes.length) {
      // Still bind copy + fullscreen buttons so they can hide
      // themselves gracefully on pages without a zoomable image.
      bindCopyButtons();
      bindFullscreen();
      return;
    }
    let firstViewer = null;
    for (const el of nodes) {
      if (el.dataset.zoomBound === '1') continue;
      el.dataset.zoomBound = '1';
      const viewer = createViewer(el);
      if (firstViewer === null) firstViewer = viewer;
    }
    if (firstViewer) applyDeepLink(firstViewer);
    bindCopyButtons();
    bindFullscreen();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
