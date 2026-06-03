/*
 * Persona — keyboard jump-to-result on /search (v0.51).
 *
 * Vanilla ES2020. No frameworks.
 *
 * Bindings (active only on /search and only when focus is NOT in a text
 * field / contenteditable):
 *   j  — move highlight to the next result
 *   k  — move highlight to the previous result
 *   Enter — open the first <a href> inside the highlighted result
 *   /  — focus the search query input
 *
 * Results are discovered live via the [data-search-result] selector, so
 * htmx swaps inside #results keep working without re-binding. The active
 * card is marked with the .search-result-active CSS class and scrolled
 * into view (block: "nearest") so the page does not jump if the card is
 * already visible.
 */
(function () {
  "use strict";

  // Only run on the /search page. Pathname compare is exact-prefix so
  // /search and /search/whatever both qualify, but /search-foo does not.
  function isSearchPage() {
    const p = window.location.pathname;
    return p === "/search" || p.startsWith("/search/") || p.startsWith("/search?");
  }
  if (!isSearchPage()) return;

  const ACTIVE_CLASS = "search-result-active";
  const RESULT_SELECTOR = "[data-search-result]";

  // v1.2 — user-customisable binding for the focus-search shortcut.
  // Defaults to "/" so a fresh install behaves identically to v0.51.
  // The j / k jump-to-result keys and Enter-to-open are not exposed in
  // the editor — they are inherent to the result-list interaction
  // pattern (Gmail / Vim style) rather than the cheatsheet shortcuts
  // the editor surfaces. Loaded asynchronously below; a failed fetch
  // leaves the binding at its default.
  const DEFAULT_SEARCH_KEY = "/";
  let searchKey = DEFAULT_SEARCH_KEY;

  /** @type {Element | null} */
  let activeEl = null;

  // -------------------------------------------------------------------
  // Focus guard — never hijack keystrokes the user is typing into a field.
  // -------------------------------------------------------------------
  function isTyping(target) {
    if (!target || target.nodeType !== 1) return false;
    const el = /** @type {HTMLElement} */ (target);
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  /** @returns {Element[]} */
  function getResults() {
    return Array.from(document.querySelectorAll(RESULT_SELECTOR));
  }

  /** @returns {HTMLInputElement | null} */
  function getQueryInput() {
    return /** @type {HTMLInputElement | null} */ (
      document.querySelector('form input[name="q"]')
    );
  }

  // -------------------------------------------------------------------
  // Highlight management
  // -------------------------------------------------------------------
  function setActive(el) {
    if (activeEl && activeEl !== el) {
      activeEl.classList.remove(ACTIVE_CLASS);
    }
    activeEl = el;
    if (!el) return;
    el.classList.add(ACTIVE_CLASS);
    // "nearest" avoids the jarring re-centering when the row is already
    // on screen — only scrolls when truly off-screen.
    try {
      el.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (_e) {
      // Older browsers without the options object — fall back to default.
      el.scrollIntoView();
    }
  }

  function move(delta) {
    const results = getResults();
    if (results.length === 0) return;

    // If the previously-active element was removed (htmx swap), drop it.
    if (activeEl && !document.body.contains(activeEl)) {
      activeEl = null;
    }

    let idx = activeEl ? results.indexOf(activeEl) : -1;
    if (idx === -1) {
      // No selection yet: j enters at top, k enters at bottom.
      idx = delta > 0 ? 0 : results.length - 1;
    } else {
      idx = (idx + delta + results.length) % results.length;
    }
    setActive(results[idx]);
  }

  function openActive() {
    if (!activeEl) return false;
    const link = activeEl.querySelector("a[href]");
    if (!link) return false;
    // Prefer a real navigation so middle-click-style modifiers behave
    // sensibly via the browser. Synthesising a click respects target=_blank.
    /** @type {HTMLAnchorElement} */ (link).click();
    return true;
  }

  // -------------------------------------------------------------------
  // Key handler
  // -------------------------------------------------------------------
  document.addEventListener(
    "keydown",
    (ev) => {
      // Bound focus-search shortcut (default "/"). Allowed only while
      // typing is NOT happening so it does not eat the literal token
      // from inside another text box. Sequence bindings ("g s") are
      // ignored here — they are the cheatsheet listener's territory.
      const boundSearch = (searchKey || DEFAULT_SEARCH_KEY).trim();
      const isSequence = boundSearch.indexOf(" ") !== -1;
      if (!isSequence && ev.key === boundSearch && !isTyping(ev.target)) {
        const input = getQueryInput();
        if (input) {
          ev.preventDefault();
          input.focus();
          // Move caret to the end of the existing query so the user can
          // keep typing without erasing what's there.
          const len = input.value.length;
          try {
            input.setSelectionRange(len, len);
          } catch (_e) {
            // Some input types reject setSelectionRange — ignore.
          }
        }
        return;
      }

      if (isTyping(ev.target)) return;

      // Ignore when a modifier is held — those belong to the browser /
      // command palette.
      if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

      if (ev.key === "j") {
        ev.preventDefault();
        move(1);
      } else if (ev.key === "k") {
        ev.preventDefault();
        move(-1);
      } else if (ev.key === "Enter") {
        if (openActive()) {
          ev.preventDefault();
        }
      }
    },
    false,
  );

  // -------------------------------------------------------------------
  // htmx integration — drop the stale highlight after a results swap so
  // the next j/k starts fresh from the top.
  // -------------------------------------------------------------------
  document.body.addEventListener("htmx:afterSwap", (ev) => {
    const detail = /** @type {any} */ (ev).detail;
    if (!detail || !detail.target) return;
    if (detail.target.id === "results" || detail.target.closest("#results")) {
      activeEl = null;
    }
  });

  // -------------------------------------------------------------------
  // v1.2 — load the user's bound focus-search key. Best-effort fetch:
  // a failure leaves searchKey at the default so "/" still focuses the
  // input exactly like the v0.51 behaviour.
  // -------------------------------------------------------------------
  fetch("/api/kbd-shortcuts.json", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then((res) => (res.ok ? res.json() : null))
    .then((json) => {
      if (!json || typeof json !== "object") return;
      const value = json.search_focus;
      if (typeof value === "string" && value.trim()) {
        searchKey = value.trim();
      }
    })
    .catch(() => {
      /* keep default */
    });
})();
