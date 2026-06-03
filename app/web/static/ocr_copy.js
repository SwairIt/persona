/*
 * ocr_copy.js — v0.67 feature 2/3.
 *
 * Wires up "Copy OCR text" buttons on the screenshot detail page.
 *
 * Activation: any element with a `data-copy-ocr` attribute becomes a click
 * target. On click we resolve the text to copy in this order:
 *   1. `data-ocr-text` attribute on the button (preferred — survives any
 *      markup the source block uses, including <pre> with embedded markup).
 *   2. `textContent` of the element pointed at by `data-copy-target`
 *      (CSS selector), if provided.
 *   3. Otherwise the button's own `textContent`.
 *
 * After a successful clipboard write we briefly attach a small "Copied!"
 * tooltip next to the button (absolutely positioned, auto-removed after
 * ~1.5s). On failure (insecure context, permission denied, no Clipboard
 * API) we surface a "Copy failed" tooltip instead — and as a last resort
 * fall back to a hidden <textarea> + document.execCommand('copy') so the
 * button still works on older browsers / non-HTTPS dev environments.
 *
 * Event delegation on document means the handler keeps working even if
 * buttons are swapped in later by htmx.
 */
(function () {
  'use strict';

  /** Resolve the OCR text to copy from a triggering button. */
  function resolveText(btn) {
    if (btn.hasAttribute('data-ocr-text')) {
      return btn.getAttribute('data-ocr-text') || '';
    }
    const sel = btn.getAttribute('data-copy-target');
    if (sel) {
      const target = document.querySelector(sel);
      if (target) return target.textContent || '';
    }
    return btn.textContent || '';
  }

  /**
   * Briefly show a floating tooltip near the button. We position absolutely
   * relative to the document so the tooltip never gets clipped by a
   * scrollable parent / overflow:hidden container.
   */
  function flashTooltip(btn, message, ok) {
    // Remove any prior tooltip we attached to this button so rapid clicks
    // don't pile multiple bubbles on top of each other.
    const prior = btn._ocrCopyTip;
    if (prior && prior.parentNode) prior.parentNode.removeChild(prior);

    const tip = document.createElement('span');
    tip.textContent = message;
    tip.setAttribute('role', 'status');
    tip.style.position = 'absolute';
    tip.style.zIndex = '9999';
    tip.style.padding = '3px 8px';
    tip.style.borderRadius = '4px';
    tip.style.fontSize = '11px';
    tip.style.fontFamily = 'ui-sans-serif, system-ui, sans-serif';
    tip.style.lineHeight = '1';
    tip.style.pointerEvents = 'none';
    tip.style.color = '#fff';
    tip.style.background = ok ? '#059669' : '#dc2626';
    tip.style.boxShadow = '0 2px 6px rgba(0,0,0,0.35)';
    tip.style.transition = 'opacity 200ms ease';
    tip.style.opacity = '0';

    const rect = btn.getBoundingClientRect();
    const scrollX = window.scrollX || window.pageXOffset || 0;
    const scrollY = window.scrollY || window.pageYOffset || 0;
    // Anchor above the button, horizontally centred.
    tip.style.left = (rect.left + scrollX + rect.width / 2) + 'px';
    tip.style.top = (rect.top + scrollY - 6) + 'px';
    tip.style.transform = 'translate(-50%, -100%)';

    document.body.appendChild(tip);
    btn._ocrCopyTip = tip;

    // Force a reflow so the opacity transition triggers.
    void tip.offsetWidth;
    tip.style.opacity = '1';

    window.setTimeout(() => {
      tip.style.opacity = '0';
      window.setTimeout(() => {
        if (tip.parentNode) tip.parentNode.removeChild(tip);
        if (btn._ocrCopyTip === tip) btn._ocrCopyTip = null;
      }, 220);
    }, 1300);
  }

  /** execCommand-based fallback for non-secure contexts. */
  function legacyCopy(text) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '-1000px';
      ta.style.left = '-1000px';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (_err) {
      return false;
    }
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext !== false) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_err) {
        // Fall through to legacy path.
      }
    }
    return legacyCopy(text);
  }

  document.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const btn = target.closest('[data-copy-ocr]');
    if (!btn) return;

    event.preventDefault();
    const text = resolveText(btn);
    const ok = await copyText(text);
    flashTooltip(btn, ok ? 'Copied!' : 'Copy failed', ok);
  });
})();
