/*
 * ocr_highlight.js — Persona v0.72 feature 1/3.
 *
 * When the screenshot page is opened with `?highlight=<keyword>` in the
 * query string, draw translucent yellow rectangles over every OCR word
 * box whose text matches the keyword case-insensitively.
 *
 * Wiring:
 *   - Reads ?highlight= from window.location.search.
 *   - Pulls word boxes from /api/screenshot/{id}/words.json (the JSON
 *     sibling of the v0.35 overlay endpoint). Screenshot id comes from
 *     the [data-shot-id] attribute on the sticky-host wrapper rendered
 *     by screenshot.html.
 *   - Word boxes (left/top/width/height in source-image pixel space) are
 *     translated to percentages of the image's natural dimensions, so a
 *     single CSS `inset: 0` layer that mirrors the image's CSS transform
 *     stays pixel-aligned whether the user has zoomed/panned or not.
 *
 * Graceful failure modes (all silent, no console noise on the happy path):
 *   - ?highlight missing or empty -> no-op.
 *   - No zoomable image on the page (thumbnail reclaimed by retention)
 *     -> no-op; nothing to anchor to.
 *   - words.json returns 404 / non-JSON / { words: [] } -> no-op.
 *   - Individual word rows missing geometry (any of left/top/width/height
 *     is null) -> skipped; the rest still render.
 *
 * The script is loaded with `defer` from screenshot.html, so the DOM is
 * already parsed by the time it runs.
 *
 * No framework. Vanilla ES2020. No dependencies on image_viewer.js
 * internals — we sync to the image's CSS transform via MutationObserver
 * on the `style` attribute, which the zoom viewer writes to.
 */
(function () {
  'use strict';

  /** Read `?highlight=` from the current URL. Returns null when missing/empty. */
  function readHighlightParam() {
    const params = new URLSearchParams(window.location.search);
    if (!params.has('highlight')) return null;
    const raw = params.get('highlight');
    if (!raw) return null;
    const trimmed = raw.trim();
    return trimmed ? trimmed : null;
  }

  /**
   * Pull the screenshot id from the sticky-host wrapper. We deliberately
   * do NOT parse it out of `location.pathname` — the route happens to be
   * /screenshot/{id} today but that's a URL choice; the data-attribute
   * is the contract we control.
   */
  function readShotId() {
    const host = document.querySelector('[data-sticky-host][data-shot-id]');
    if (!host) return null;
    const raw = host.getAttribute('data-shot-id');
    if (!raw) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? n : null;
  }

  /**
   * Compare a single OCR word to the highlight keyword. Case-insensitive
   * substring match — the v0.35 word table stores one token per row, so
   * `word === keyword` would miss `Foo,` vs `Foo`. We also strip a small
   * set of leading/trailing punctuation that Tesseract leaves attached.
   */
  function wordMatches(word, keywordLower) {
    if (!word) return false;
    const lower = String(word).toLowerCase();
    if (lower.includes(keywordLower)) return true;
    // Strip a handful of common punctuation and retry — catches "Foo!"
    // when the user searched for "foo" without dragging the punctuation
    // along.
    const stripped = lower.replace(/^[\s.,;:!?"'()\[\]{}<>]+|[\s.,;:!?"'()\[\]{}<>]+$/g, '');
    return stripped !== lower && stripped.includes(keywordLower);
  }

  /**
   * Build the absolutely-positioned overlay layer that sits over the
   * [data-zoomable] image. We mirror the image's CSS transform onto the
   * layer so zoom/pan move the rectangles in lockstep. transform-origin
   * is 0,0 to match image_viewer.css.
   */
  function createOverlay(img) {
    const layer = document.createElement('div');
    layer.className = 'ocr-highlight-layer';
    layer.style.position = 'absolute';
    layer.style.left = '0';
    layer.style.top = '0';
    layer.style.right = '0';
    layer.style.bottom = '0';
    layer.style.pointerEvents = 'none';
    layer.style.transformOrigin = '0 0';
    // Match the image's current transform on first paint; observer below
    // keeps it in sync after that.
    layer.style.transform = img.style.transform || '';
    return layer;
  }

  /**
   * Draw one yellow rectangle for `word`. Coordinates arrive in the
   * source-image native pixel space (Tesseract's bbox), so we convert
   * to percentages of naturalWidth/naturalHeight — that way the overlay
   * layer's `inset: 0` (== same box as the rendered <img>) plus the
   * matching CSS transform keep the rectangle pinned to the right text.
   */
  function drawBox(layer, word, naturalW, naturalH) {
    if (word.left === null || word.top === null) return;
    if (word.width === null || word.height === null) return;
    if (!naturalW || !naturalH) return;
    const box = document.createElement('div');
    box.className = 'ocr-highlight-box';
    box.style.position = 'absolute';
    box.style.left = (word.left / naturalW * 100).toFixed(3) + '%';
    box.style.top = (word.top / naturalH * 100).toFixed(3) + '%';
    box.style.width = (word.width / naturalW * 100).toFixed(3) + '%';
    box.style.height = (word.height / naturalH * 100).toFixed(3) + '%';
    // Translucent yellow — readable over both light and dark screenshots.
    // 1px solid border keeps very small word boxes visible even when the
    // fill blends into bright UI chrome.
    box.style.background = 'rgba(250, 204, 21, 0.40)';
    box.style.border = '1px solid rgba(250, 204, 21, 0.90)';
    box.style.boxSizing = 'border-box';
    box.style.borderRadius = '2px';
    box.style.pointerEvents = 'none';
    box.title = word.word + ' · conf=' + word.conf;
    layer.appendChild(box);
  }

  /**
   * Mirror the image's CSS transform onto the overlay layer. The zoom
   * viewer in image_viewer.js writes the transform string directly onto
   * the <img> element's `style.transform`, so the cheapest sync is a
   * MutationObserver on the `style` attribute.
   */
  function bindTransformSync(img, layer) {
    const obs = new MutationObserver(() => {
      layer.style.transform = img.style.transform || '';
    });
    obs.observe(img, { attributes: true, attributeFilter: ['style'] });
  }

  /**
   * Once the image has reported its natural dimensions, build the overlay
   * and append it to the same `.zoom-wrapper` parent so it inherits the
   * wrapper's overflow clipping. Stops cleanly if any precondition fails.
   */
  function renderHighlights(img, words, keywordLower) {
    const wrapper = img.closest('.zoom-wrapper');
    if (!wrapper) return 0;
    const naturalW = img.naturalWidth;
    const naturalH = img.naturalHeight;
    if (!naturalW || !naturalH) return 0;

    const matches = [];
    for (const w of words) {
      if (wordMatches(w.word, keywordLower)) matches.push(w);
    }
    if (!matches.length) return 0;

    const layer = createOverlay(img);
    for (const w of matches) {
      drawBox(layer, w, naturalW, naturalH);
    }
    wrapper.appendChild(layer);
    bindTransformSync(img, layer);
    return matches.length;
  }

  /**
   * Fetch words.json and kick off rendering. We don't surface failures to
   * the user — this is a best-effort hint, not a primary feature.
   */
  async function run() {
    const keyword = readHighlightParam();
    if (!keyword) return;
    const shotId = readShotId();
    if (!shotId) return;
    const img = document.querySelector('[data-zoomable]');
    if (!img) return;

    let payload;
    try {
      const r = await fetch('/api/screenshot/' + shotId + '/words.json', {
        credentials: 'same-origin',
      });
      if (!r.ok) return;
      payload = await r.json();
    } catch (_) {
      return;
    }
    if (!payload || !Array.isArray(payload.words) || !payload.words.length) return;

    const keywordLower = keyword.toLowerCase();
    const draw = () => renderHighlights(img, payload.words, keywordLower);
    if (img.complete && img.naturalWidth) {
      draw();
    } else {
      img.addEventListener('load', draw, { once: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run, { once: true });
  } else {
    run();
  }
})();
