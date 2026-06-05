/* mobile_bottom_nav.js — opt-in bottom-nav loader.
 *
 * Drop this in by adding ONE line to base.html (or any layout):
 *
 *     <script src="/static/mobile_bottom_nav.js" defer></script>
 *
 * The script is deliberately kept out of base.html by default — the
 * task says be conservative with the global shell. Including it
 * manually is the documented opt-in path that avoids any risk of
 * silently changing the desktop nav for every operator.
 *
 * Flow:
 *   1. On DOMContentLoaded, GET /api/mobile-bottom-nav.json (or, since
 *      we only expose the POST endpoint, GET /widget/mobile-bottom-nav
 *      directly and inject it). To avoid an extra round-trip we just
 *      check a server-rendered data attribute first: if the body carries
 *      `data-mobile-bottom-nav="1"` we inject; otherwise we no-op.
 *   2. Inject the fragment at the end of <body>. We pass the current
 *      page's active_nav slug via the URL so the right icon highlights.
 *      The slug is read from `document.body.dataset.activeNav` if
 *      present — falls back to no highlight when missing.
 *
 * The script is a self-contained IIFE. No HTMX or Alpine required at
 * load time; it uses plain `fetch` + `insertAdjacentHTML`.
 */
(function () {
  'use strict';

  function readActiveNav() {
    var body = document.body;
    if (!body) return '';
    // Two compatible sources: an explicit dataset, or a page-scoped
    // window.PERSONA_ACTIVE_NAV global some older templates set.
    if (body.dataset && body.dataset.activeNav) {
      return String(body.dataset.activeNav);
    }
    if (typeof window.PERSONA_ACTIVE_NAV === 'string') {
      return window.PERSONA_ACTIVE_NAV;
    }
    return '';
  }

  function isEnabled() {
    // The base.html opt-in writes data-mobile-bottom-nav="1" on <body>.
    // Without it, do nothing — this script is safe to ship globally
    // because operators who never set the flag never see the bar.
    var body = document.body;
    return !!(body && body.dataset && body.dataset.mobileBottomNav === '1');
  }

  function injectBar() {
    if (!isEnabled()) return;
    // Idempotency guard — re-running the loader (e.g. after an HTMX
    // boost reload) must not produce two stacked bars.
    if (document.querySelector('nav[aria-label="mobile-bottom-nav"]')) return;
    var qs = '?active=' + encodeURIComponent(readActiveNav());
    fetch('/widget/mobile-bottom-nav' + qs, { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        if (!isEnabled()) return; // racey toggle-off while in flight
        document.body.insertAdjacentHTML('beforeend', html);
      })
      .catch(function (e) {
        // Swallow — the bar is cosmetic; a network blip must not break
        // the host page. Surface to the console for debugging.
        if (window.console && window.console.warn) {
          window.console.warn('mobile_bottom_nav: load failed', e);
        }
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectBar);
  } else {
    injectBar();
  }
})();
