/*
 * sticky_notes.js — Persona v0.64
 *
 * Per-shot sticky-note overlay for the screenshot detail page.
 *
 * Behaviour:
 *   - On page load, fetch /api/screenshot/{id}/sticky.json and render every
 *     sticky as an absolutely-positioned <div> inside the .sticky-layer
 *     overlay that sits on top of the screenshot image.
 *   - Double-click anywhere on the image (or the overlay) creates a new
 *     sticky at the click position: a `prompt()` collects the body text,
 *     then we POST it and re-render. Empty / cancelled prompt is a no-op.
 *   - Each sticky has a small ✕ button that POSTs to the delete endpoint
 *     and removes the node on success.
 *
 * Positioning is fractional: x_pct / y_pct live in [0, 1] and are
 * translated to `left: X%; top: Y%` so the same sticky keeps its visual
 * position when the wrapper is resized (responsive layout, zoom, etc.).
 *
 * The script is a no-op on pages that lack #sticky-host — it walks away
 * silently rather than throwing, so it can be safely loaded site-wide.
 *
 * Pure ES2020, no framework. Talks JSON to the FastAPI routes defined in
 * app/web/routes/sticky_notes.py.
 */
(function () {
  'use strict';

  /** Read the screenshot id + endpoints from the wrapper's data-* attrs. */
  function readConfig(host) {
    const shotId = parseInt(host.dataset.shotId || '0', 10);
    if (!Number.isFinite(shotId) || shotId <= 0) return null;
    return {
      shotId: shotId,
      listUrl: `/api/screenshot/${shotId}/sticky.json`,
      createUrl: `/api/screenshot/${shotId}/sticky`,
      deleteUrl: (id) => `/api/sticky/${id}/delete`,
    };
  }

  /** Escape a string for safe insertion into innerHTML. */
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    }[c]));
  }

  /** Clamp `n` into [0, 1]. */
  function clamp01(n) {
    if (!Number.isFinite(n)) return 0;
    if (n < 0) return 0;
    if (n > 1) return 1;
    return n;
  }

  /** Build the DOM node for one sticky. */
  function renderSticky(layer, cfg, sticky) {
    const node = document.createElement('div');
    node.className = 'sticky-note';
    node.dataset.stickyId = String(sticky.id);
    node.style.position = 'absolute';
    node.style.left = (clamp01(sticky.x_pct) * 100).toFixed(3) + '%';
    node.style.top = (clamp01(sticky.y_pct) * 100).toFixed(3) + '%';
    node.style.transform = 'translate(-50%, -50%)';
    node.style.background = colorToCss(sticky.color);
    node.style.color = '#111';
    node.style.padding = '6px 22px 6px 8px';
    node.style.borderRadius = '4px';
    node.style.fontSize = '12px';
    node.style.lineHeight = '1.35';
    node.style.maxWidth = '180px';
    node.style.boxShadow = '0 2px 6px rgba(0, 0, 0, 0.35)';
    node.style.whiteSpace = 'pre-wrap';
    node.style.wordBreak = 'break-word';
    node.style.pointerEvents = 'auto';
    node.style.cursor = 'default';
    node.style.userSelect = 'text';
    node.style.zIndex = '5';

    const body = document.createElement('span');
    body.textContent = sticky.body;
    node.appendChild(body);

    const close = document.createElement('button');
    close.type = 'button';
    close.textContent = '×';
    close.title = 'Delete sticky';
    close.setAttribute('aria-label', 'Delete sticky note');
    close.style.position = 'absolute';
    close.style.top = '1px';
    close.style.right = '3px';
    close.style.background = 'transparent';
    close.style.border = '0';
    close.style.color = '#333';
    close.style.cursor = 'pointer';
    close.style.fontSize = '14px';
    close.style.lineHeight = '1';
    close.style.padding = '2px 4px';
    close.addEventListener('click', (e) => {
      e.stopPropagation();
      void deleteSticky(layer, cfg, sticky.id, node);
    });
    node.appendChild(close);

    // Swallow dblclick so deleting "near" a sticky doesn't immediately
    // create a new sticky on top of the old one.
    node.addEventListener('dblclick', (e) => e.stopPropagation());

    layer.appendChild(node);
  }

  /** Map a colour name from the server to a CSS background. */
  function colorToCss(name) {
    const palette = {
      yellow: '#fff59d',
      pink:   '#f8bbd0',
      blue:   '#bbdefb',
      green:  '#c8e6c9',
      orange: '#ffe0b2',
    };
    if (typeof name === 'string' && name.startsWith('#')) return name;
    return palette[name] || palette.yellow;
  }

  /** Wipe the overlay layer of all sticky nodes. */
  function clearLayer(layer) {
    const nodes = layer.querySelectorAll('.sticky-note');
    nodes.forEach((n) => n.remove());
  }

  /** Fetch + render every sticky for this screenshot. */
  async function loadStickies(layer, cfg) {
    let items = [];
    try {
      const r = await fetch(cfg.listUrl, { credentials: 'same-origin' });
      if (!r.ok) return;
      items = await r.json();
    } catch (_err) {
      return;
    }
    if (!Array.isArray(items)) return;
    clearLayer(layer);
    items.forEach((sticky) => renderSticky(layer, cfg, sticky));
  }

  /** POST a new sticky and re-render on success. */
  async function createSticky(layer, cfg, xPct, yPct, body) {
    const fd = new FormData();
    fd.append('x_pct', String(xPct));
    fd.append('y_pct', String(yPct));
    fd.append('body', body);
    fd.append('color', 'yellow');
    let r;
    try {
      r = await fetch(cfg.createUrl, {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
      });
    } catch (_err) {
      return;
    }
    if (!r.ok) return;
    let sticky = null;
    try {
      sticky = await r.json();
    } catch (_err) {
      sticky = null;
    }
    if (sticky && typeof sticky === 'object') {
      renderSticky(layer, cfg, sticky);
    } else {
      await loadStickies(layer, cfg);
    }
  }

  /** POST delete and drop the node on success. */
  async function deleteSticky(layer, cfg, stickyId, node) {
    let r;
    try {
      r = await fetch(cfg.deleteUrl(stickyId), {
        method: 'POST',
        credentials: 'same-origin',
      });
    } catch (_err) {
      return;
    }
    if (r.ok && node && node.parentNode) {
      node.parentNode.removeChild(node);
    }
  }

  /** Compute the click position as a (0..1, 0..1) fraction of the layer. */
  function clickToFraction(layer, evt) {
    const rect = layer.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    const x = (evt.clientX - rect.left) / rect.width;
    const y = (evt.clientY - rect.top) / rect.height;
    return { x: clamp01(x), y: clamp01(y) };
  }

  /** Wire the dblclick handler that spawns a new sticky via prompt(). */
  function bindCreate(host, layer, cfg) {
    host.addEventListener('dblclick', (evt) => {
      if (evt.target && evt.target.closest && evt.target.closest('.sticky-note')) {
        return;
      }
      const pos = clickToFraction(layer, evt);
      if (!pos) return;
      const raw = window.prompt('Sticky note text:');
      if (raw === null) return;
      const body = raw.trim();
      if (!body) return;
      void createSticky(layer, cfg, pos.x, pos.y, body);
    });
  }

  /** Find every sticky host on the page and wire it up. */
  function init() {
    const hosts = document.querySelectorAll('[data-sticky-host="1"]');
    hosts.forEach((host) => {
      if (host.dataset.stickyBound === '1') return;
      const cfg = readConfig(host);
      if (!cfg) return;

      let layer = host.querySelector('.sticky-layer');
      if (!layer) {
        layer = document.createElement('div');
        layer.className = 'sticky-layer';
        layer.style.position = 'absolute';
        layer.style.inset = '0';
        layer.style.pointerEvents = 'none';
        host.appendChild(layer);
      }
      // Ensure the wrapper itself is a positioning context.
      const computed = window.getComputedStyle(host);
      if (computed.position === 'static') {
        host.style.position = 'relative';
      }

      host.dataset.stickyBound = '1';
      bindCreate(host, layer, cfg);
      void loadStickies(layer, cfg);

      // Re-render on a global "refresh-stickies" event so external code
      // (e.g. an htmx delete elsewhere) can trigger a reload.
      document.body.addEventListener('refresh-stickies', () => {
        void loadStickies(layer, cfg);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
