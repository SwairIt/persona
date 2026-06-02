/*
 * image_viewer.js — Persona v0.46
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

    // Initial paint so the transform string exists even before interaction.
    apply();
  }

  function init() {
    const nodes = document.querySelectorAll('[data-zoomable]');
    if (!nodes.length) return; // graceful no-op when no zoomable image present
    for (const el of nodes) {
      if (el.dataset.zoomBound === '1') continue;
      el.dataset.zoomBound = '1';
      createViewer(el);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();
