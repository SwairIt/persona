/*
 * Persona — keyboard shortcuts cheatsheet (v0.39).
 *
 * Vanilla ES2020. No frameworks.
 *
 * Press "?" anywhere outside a text input to open the cheatsheet. Escape
 * closes it. Multi-key "g+letter" sequences resolve within a 1500ms
 * window — typing "g" then "t" within that window navigates to /timeline.
 *
 * Style: minimal — see keyboard_shortcuts.css. The DOM is built
 * imperatively so the file stays framework-free and matches the style
 * of command_palette.js.
 */
(function () {
  "use strict";

  const SEQUENCE_TIMEOUT_MS = 1500;

  /** @typedef {{ keys: string, label: string, group: string }} Shortcut */

  /** @type {Shortcut[]} */
  const SHORTCUTS = [
    { keys: "?", label: "Show this help", group: "General" },
    { keys: "Cmd / Ctrl + K", label: "Open command palette", group: "General" },
    { keys: "/", label: "Focus search input (on /search)", group: "General" },
    { keys: "Esc", label: "Close modal / cheatsheet", group: "General" },
    { keys: "g then t", label: "Go to Timeline", group: "Navigate" },
    { keys: "g then s", label: "Go to Search", group: "Navigate" },
    { keys: "g then h", label: "Go to Heatmap", group: "Navigate" },
    { keys: "g then f", label: "Go to Focus", group: "Navigate" },
  ];

  /** @type {Record<string, string>} */
  const GO_TARGETS = {
    t: "/timeline",
    s: "/search",
    h: "/heatmap",
    f: "/focus",
  };

  // v1.2 — user-customisable bindings. Loaded once on init from
  // /api/kbd-shortcuts.json; falls back to the historical defaults if
  // the fetch fails so an offline / errored response leaves listeners
  // working with their original behaviour. Only the four actions wired
  // up in this file are honoured here — go_timeline overrides the
  // "g t" sequence specifically, the other g+letter combos stay on
  // their built-in mappings so search/heatmap/focus still navigate.
  const DEFAULT_BINDINGS = {
    help_overlay: "?",
    search_focus: "/",
    go_timeline: "g t",
  };
  /** @type {{ help_overlay: string, search_focus: string, go_timeline: string }} */
  let bindings = Object.assign({}, DEFAULT_BINDINGS);

  /** Split a binding string into one or two whitespace-separated tokens. */
  function tokenise(binding) {
    return String(binding || "").trim().split(/\s+/).filter(Boolean);
  }

  /** @type {HTMLDivElement | null} */
  let overlayEl = null;
  /** @type {number | null} */
  let sequenceTimer = null;
  /** @type {string | null} */
  let pendingPrefix = null;

  // ---------------------------------------------------------------------
  // Focus guard — never hijack keystrokes the user is typing into a field.
  // ---------------------------------------------------------------------

  function isTyping(target) {
    if (!target || target.nodeType !== 1) return false;
    const el = /** @type {HTMLElement} */ (target);
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  // ---------------------------------------------------------------------
  // Modal DOM — built once on first open, then reused.
  // ---------------------------------------------------------------------

  function buildOverlay() {
    const overlay = document.createElement("div");
    overlay.className = "persona-kbd-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Keyboard shortcuts");
    overlay.hidden = true;

    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) close();
    });

    const modal = document.createElement("div");
    modal.className = "persona-kbd-modal";

    const header = document.createElement("div");
    header.className = "persona-kbd-header";

    const title = document.createElement("h2");
    title.className = "persona-kbd-title";
    title.textContent = "Keyboard shortcuts";
    header.appendChild(title);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "persona-kbd-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "✕";
    closeBtn.addEventListener("click", close);
    header.appendChild(closeBtn);

    modal.appendChild(header);

    // Group shortcuts by section in stable insertion order.
    /** @type {Map<string, Shortcut[]>} */
    const groups = new Map();
    for (const sc of SHORTCUTS) {
      const arr = groups.get(sc.group);
      if (arr) arr.push(sc);
      else groups.set(sc.group, [sc]);
    }

    for (const [group, items] of groups) {
      const section = document.createElement("section");
      section.className = "persona-kbd-section";

      const heading = document.createElement("h3");
      heading.className = "persona-kbd-group";
      heading.textContent = group;
      section.appendChild(heading);

      const list = document.createElement("dl");
      list.className = "persona-kbd-list";

      for (const item of items) {
        const row = document.createElement("div");
        row.className = "persona-kbd-row";

        const dt = document.createElement("dt");
        dt.className = "persona-kbd-keys";
        // Each whitespace-separated token becomes its own <kbd>; the "+"
        // and "then" tokens are rendered inline as separators.
        const tokens = item.keys.split(/\s+/);
        tokens.forEach((tok, idx) => {
          if (tok === "+" || tok === "then" || tok === "/") {
            const sep = document.createElement("span");
            sep.className = "persona-kbd-sep";
            sep.textContent = tok;
            dt.appendChild(sep);
          } else {
            const kbd = document.createElement("kbd");
            kbd.className = "persona-kbd-key";
            kbd.textContent = tok;
            dt.appendChild(kbd);
          }
          if (idx < tokens.length - 1) {
            dt.appendChild(document.createTextNode(" "));
          }
        });

        const dd = document.createElement("dd");
        dd.className = "persona-kbd-label";
        dd.textContent = item.label;

        row.appendChild(dt);
        row.appendChild(dd);
        list.appendChild(row);
      }

      section.appendChild(list);
      modal.appendChild(section);
    }

    const footer = document.createElement("div");
    footer.className = "persona-kbd-footer";
    footer.textContent = "Press Esc to close";
    modal.appendChild(footer);

    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    return overlay;
  }

  function ensureOverlay() {
    if (!overlayEl) overlayEl = buildOverlay();
    return overlayEl;
  }

  function isOpen() {
    return overlayEl !== null && overlayEl.hidden === false;
  }

  function open() {
    const el = ensureOverlay();
    if (el.hidden === false) return;
    el.hidden = false;
    document.documentElement.classList.add("persona-kbd-open");
    const closeBtn = el.querySelector(".persona-kbd-close");
    if (closeBtn instanceof HTMLElement) closeBtn.focus();
  }

  function close() {
    if (!overlayEl || overlayEl.hidden) return;
    overlayEl.hidden = true;
    document.documentElement.classList.remove("persona-kbd-open");
  }

  // ---------------------------------------------------------------------
  // Multi-key "g + letter" sequence handling.
  // ---------------------------------------------------------------------

  function clearSequence() {
    pendingPrefix = null;
    if (sequenceTimer !== null) {
      window.clearTimeout(sequenceTimer);
      sequenceTimer = null;
    }
  }

  function startSequence(prefix) {
    clearSequence();
    pendingPrefix = prefix;
    sequenceTimer = window.setTimeout(clearSequence, SEQUENCE_TIMEOUT_MS);
  }

  // ---------------------------------------------------------------------
  // Global keydown listener.
  // ---------------------------------------------------------------------

  function onKeyDown(ev) {
    // Escape always closes the cheatsheet, even from inside the modal's
    // focused close-button.
    if (ev.key === "Escape" && isOpen()) {
      ev.preventDefault();
      close();
      return;
    }

    // Never hijack a keystroke aimed at an input/textarea/contenteditable.
    if (isTyping(ev.target)) return;

    // Ignore raw modifier presses and any combo with Ctrl/Meta/Alt — those
    // belong to the command palette and the browser.
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;

    const key = ev.key;

    // Resolve the user's bound keys; tokenise() always returns at least
    // one entry because the fallback defaults are non-empty.
    const helpTokens = tokenise(bindings.help_overlay);
    const searchTokens = tokenise(bindings.search_focus);
    const timelineTokens = tokenise(bindings.go_timeline);
    const helpKey = helpTokens[0] || "?";
    const searchKey = searchTokens[0] || "/";
    // The go_timeline binding may be a sequence ("g t") or a single
    // instant key (e.g. "T"). When it's a sequence we keep the legacy
    // GO_TARGETS map for the trailing letter so /search, /heatmap,
    // /focus still navigate via g+s, g+h, g+f.
    const timelinePrefix = timelineTokens.length === 2 ? timelineTokens[0] : null;
    const timelineSuffix = timelineTokens.length === 2 ? timelineTokens[1] : null;
    const timelineInstant = timelineTokens.length === 1 ? timelineTokens[0] : null;

    // Open / close the cheatsheet on the help binding (default "?").
    if (key === helpKey) {
      ev.preventDefault();
      clearSequence();
      if (isOpen()) close();
      else open();
      return;
    }

    // Don't process navigation shortcuts while the cheatsheet is open —
    // the user is reading it, not navigating.
    if (isOpen()) return;

    // Search-focus binding (default "/").
    if (key === searchKey) {
      const search = document.querySelector(
        'input[type="search"], input[name="q"], #search-input'
      );
      if (search instanceof HTMLElement) {
        ev.preventDefault();
        search.focus();
        if (search instanceof HTMLInputElement) search.select();
        clearSequence();
        return;
      }
    }

    // Instant (non-sequence) go-to-timeline binding.
    if (timelineInstant && key === timelineInstant) {
      ev.preventDefault();
      clearSequence();
      window.location.assign("/timeline");
      return;
    }

    // Multi-key sequences. The prefix arms, then a letter from
    // GO_TARGETS navigates. The user can rebind the *prefix* (e.g.
    // "j t" instead of "g t") and the same letter set still applies.
    const armedPrefix = timelinePrefix || "g";
    if (pendingPrefix === armedPrefix) {
      const lower = key.toLowerCase();
      // The bound suffix wins for the timeline target; everything else
      // (s/h/f) keeps its legacy mapping.
      const target =
        timelineSuffix && lower === timelineSuffix.toLowerCase()
          ? "/timeline"
          : GO_TARGETS[lower];
      clearSequence();
      if (target) {
        ev.preventDefault();
        window.location.assign(target);
      }
      return;
    }

    if (key === armedPrefix || key === armedPrefix.toUpperCase()) {
      startSequence(armedPrefix);
      return;
    }
  }

  // ---------------------------------------------------------------------
  // Wire-up.
  // ---------------------------------------------------------------------

  function loadBindings() {
    // Best-effort fetch — a failure (network, 404 on legacy installs
    // without the v1.2 migration applied yet, malformed JSON) leaves
    // ``bindings`` at its default values so the listeners keep working.
    return fetch("/api/kbd-shortcuts.json", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (json) {
        if (!json || typeof json !== "object") return;
        for (const action of Object.keys(DEFAULT_BINDINGS)) {
          const value = json[action];
          if (typeof value === "string" && value.trim()) {
            bindings[action] = value.trim();
          }
        }
      })
      .catch(function () { /* keep defaults */ });
  }

  function init() {
    document.addEventListener("keydown", onKeyDown);
    loadBindings();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
