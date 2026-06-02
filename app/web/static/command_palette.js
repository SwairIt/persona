/*
 * Persona — command palette (Cmd+K / Ctrl+K).
 *
 * Vanilla ES2020. No frameworks. Lazily fetches /api/palette.json the first
 * time the user opens the palette; merges that list with the recent-route
 * trail kept in localStorage under "persona.recent" (max 20 entries).
 *
 * Style: Tailwind utility classes only — same palette as base.html
 * (ink-*, accent-*, zinc-*). All DOM is built imperatively to keep the
 * file framework-free.
 */
(function () {
  "use strict";

  const RECENT_KEY = "persona.recent";
  const RECENT_MAX = 20;
  const PALETTE_ENDPOINT = "/api/palette.json";

  /** @typedef {{title: string, url: string, hint?: string, kind: string}} Item */

  /** @type {Item[] | null} */
  let cachedItems = null;
  /** @type {Promise<Item[]> | null} */
  let pendingFetch = null;

  let overlayEl = null;
  let inputEl = null;
  let listEl = null;
  let visibleItems = /** @type {Item[]} */ ([]);
  let focusIdx = 0;

  // ---------------------------------------------------------------------
  // Recent-route trail
  // ---------------------------------------------------------------------

  function readRecent() {
    try {
      const raw = window.localStorage.getItem(RECENT_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter((x) => x && typeof x.url === "string")
        .slice(0, RECENT_MAX);
    } catch (_e) {
      return [];
    }
  }

  function pushRecent(path) {
    if (!path || typeof path !== "string") return;
    try {
      const trail = readRecent().filter((x) => x.url !== path);
      trail.unshift({
        title: prettifyPath(path),
        url: path,
        kind: "recent",
      });
      window.localStorage.setItem(
        RECENT_KEY,
        JSON.stringify(trail.slice(0, RECENT_MAX)),
      );
    } catch (_e) {
      // localStorage may be disabled (private mode, quota); silently skip.
    }
  }

  function prettifyPath(path) {
    if (path === "/" || path === "") return "Timeline (/)";
    const trimmed = path.replace(/^\/+/, "").replace(/\/+$/, "");
    if (!trimmed) return "Timeline (/)";
    return trimmed
      .split("/")
      .map((seg) => seg.charAt(0).toUpperCase() + seg.slice(1).replace(/[-_]/g, " "))
      .join(" › ");
  }

  // Run once per render so every visit gets logged.
  pushRecent(window.location.pathname);

  // ---------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------

  function loadItems() {
    if (cachedItems) return Promise.resolve(cachedItems);
    if (pendingFetch) return pendingFetch;
    pendingFetch = fetch(PALETTE_ENDPOINT, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((j) => {
        const arr = Array.isArray(j && j.items) ? j.items : [];
        cachedItems = arr.filter(
          (x) => x && typeof x.url === "string" && typeof x.title === "string",
        );
        return cachedItems;
      })
      .catch(() => {
        cachedItems = [];
        return cachedItems;
      })
      .finally(() => {
        pendingFetch = null;
      });
    return pendingFetch;
  }

  function mergeWithRecent(items) {
    const recent = readRecent();
    if (recent.length === 0) return items.slice();
    const seen = new Set(recent.map((r) => r.url));
    const tail = items.filter((it) => !seen.has(it.url));
    return recent.concat(tail);
  }

  // ---------------------------------------------------------------------
  // Fuzzy match
  // ---------------------------------------------------------------------

  function fuzzyScore(needle, haystack) {
    if (!needle) return 1;
    const n = needle.toLowerCase();
    const h = haystack.toLowerCase();
    const direct = h.indexOf(n);
    if (direct === 0) return 1000;
    if (direct > 0) return 500 - direct;
    let j = 0;
    let streak = 0;
    let best = 0;
    for (let i = 0; i < n.length; i += 1) {
      const ch = n.charAt(i);
      let hit = -1;
      while (j < h.length) {
        if (h.charAt(j) === ch) {
          hit = j;
          j += 1;
          break;
        }
        j += 1;
      }
      if (hit === -1) return 0;
      streak = hit === 0 || h.charAt(hit - 1) === " " || h.charAt(hit - 1) === "-"
        ? streak + 2
        : streak + 1;
      if (streak > best) best = streak;
    }
    return 50 + best;
  }

  function scoreItem(item, query) {
    if (!query) return 1;
    const t = fuzzyScore(query, item.title);
    const u = fuzzyScore(query, item.url);
    const h = item.hint ? fuzzyScore(query, item.hint) : 0;
    return Math.max(t, Math.floor(u * 0.6), Math.floor(h * 0.8));
  }

  // ---------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------

  function kindBadge(kind) {
    const base =
      "ml-2 inline-block text-[10px] px-1.5 py-0.5 rounded uppercase tracking-wide";
    switch (kind) {
      case "recent":
        return `<span class="${base} bg-ink-700 text-zinc-400">recent</span>`;
      case "saved":
        return `<span class="${base} bg-accent-600/30 text-accent-200">saved</span>`;
      case "collection":
        return `<span class="${base} bg-emerald-600/30 text-emerald-200">coll</span>`;
      case "tag":
        return `<span class="${base} bg-sky-600/30 text-sky-200">tag</span>`;
      default:
        return "";
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function render() {
    if (!inputEl || !listEl) return;
    const q = inputEl.value.trim();
    const pool = mergeWithRecent(cachedItems || []);
    const scored = pool
      .map((item) => ({ item, score: scoreItem(item, q) }))
      .filter((x) => x.score > 0);
    if (q) {
      scored.sort((a, b) => b.score - a.score);
    }
    visibleItems = scored.slice(0, 60).map((x) => x.item);
    if (focusIdx >= visibleItems.length) focusIdx = 0;

    if (visibleItems.length === 0) {
      listEl.innerHTML =
        '<div class="px-4 py-6 text-center text-sm text-zinc-500">No matches.</div>';
      return;
    }

    const rows = visibleItems.map((item, idx) => {
      const active = idx === focusIdx;
      const cls = active
        ? "bg-accent-600/30 text-accent-100"
        : "text-zinc-300 hover:bg-ink-800";
      const hint = item.hint
        ? `<span class="ml-2 text-xs text-zinc-500">${escapeHtml(item.hint)}</span>`
        : "";
      return (
        `<a href="${escapeHtml(item.url)}" data-idx="${idx}" ` +
        `class="palette-item flex items-center justify-between px-4 py-2 rounded ${cls}">` +
        `<span class="truncate">` +
        `<span class="font-medium">${escapeHtml(item.title)}</span>` +
        hint +
        `</span>` +
        `<span class="flex items-center text-xs text-zinc-500">` +
        `<span class="font-mono">${escapeHtml(item.url)}</span>` +
        kindBadge(item.kind) +
        `</span>` +
        `</a>`
      );
    });
    listEl.innerHTML = rows.join("");

    const activeRow = listEl.querySelector(`[data-idx="${focusIdx}"]`);
    if (activeRow && typeof activeRow.scrollIntoView === "function") {
      activeRow.scrollIntoView({ block: "nearest" });
    }
  }

  // ---------------------------------------------------------------------
  // Open / close
  // ---------------------------------------------------------------------

  function ensureRoot() {
    let root = document.getElementById("palette-root");
    if (!root) {
      root = document.createElement("div");
      root.id = "palette-root";
      document.body.appendChild(root);
    }
    return root;
  }

  function open() {
    if (overlayEl) return;
    const root = ensureRoot();
    overlayEl = document.createElement("div");
    overlayEl.className =
      "persona-palette-overlay fixed inset-0 z-[60] flex items-start justify-center pt-24 px-4";
    overlayEl.innerHTML =
      '<div class="persona-palette-card bg-ink-900 border border-ink-700 rounded-lg shadow-2xl w-full max-w-2xl overflow-hidden">' +
      '  <input type="text" id="persona-palette-input" autocomplete="off" spellcheck="false"' +
      '         placeholder="Jump to…  (type to filter, ↑↓ Enter Esc)"' +
      '         class="w-full px-4 py-3 bg-transparent border-0 border-b border-ink-700' +
      '                text-zinc-100 focus:outline-none">' +
      '  <div id="persona-palette-list" class="max-h-[60vh] overflow-y-auto p-2 text-sm"></div>' +
      '  <div class="px-4 py-2 border-t border-ink-700 flex items-center justify-between text-xs text-zinc-500">' +
      '    <span>↑↓ navigate · Enter open · Esc close</span>' +
      '    <span class="font-mono">Cmd / Ctrl + K</span>' +
      '  </div>' +
      '</div>';

    overlayEl.addEventListener("click", (e) => {
      if (e.target === overlayEl) close();
    });

    root.appendChild(overlayEl);
    inputEl = overlayEl.querySelector("#persona-palette-input");
    listEl = overlayEl.querySelector("#persona-palette-list");
    focusIdx = 0;

    inputEl.addEventListener("input", () => {
      focusIdx = 0;
      render();
    });
    inputEl.addEventListener("keydown", handleKey);
    listEl.addEventListener("click", (e) => {
      const a = e.target && e.target.closest && e.target.closest(".palette-item");
      if (a) {
        // Let the browser navigate via the href; but record the click first.
        const idx = Number(a.getAttribute("data-idx"));
        if (!Number.isNaN(idx) && visibleItems[idx]) {
          pushRecent(visibleItems[idx].url);
        }
      }
    });

    // Render immediately with whatever we already have, then refresh on load.
    render();
    loadItems().then(() => render());
    inputEl.focus();
  }

  function close() {
    if (!overlayEl) return;
    overlayEl.remove();
    overlayEl = null;
    inputEl = null;
    listEl = null;
    visibleItems = [];
    focusIdx = 0;
  }

  function handleKey(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      focusIdx = Math.min(visibleItems.length - 1, focusIdx + 1);
      render();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      focusIdx = Math.max(0, focusIdx - 1);
      render();
    } else if (e.key === "Home") {
      e.preventDefault();
      focusIdx = 0;
      render();
    } else if (e.key === "End") {
      e.preventDefault();
      focusIdx = Math.max(0, visibleItems.length - 1);
      render();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const target = visibleItems[focusIdx];
      if (target) {
        pushRecent(target.url);
        window.location.href = target.url;
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }

  // ---------------------------------------------------------------------
  // Global hotkey
  // ---------------------------------------------------------------------

  function isEditableTarget(el) {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") {
      // Allow Cmd+K even inside inputs — that's the whole point.
      return false;
    }
    return Boolean(el.isContentEditable);
  }

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      if (isEditableTarget(e.target)) return;
      e.preventDefault();
      if (overlayEl) {
        close();
      } else {
        open();
      }
    }
  });

  // Expose a tiny imperative API for tests / dev console.
  window.PersonaPalette = {
    open: open,
    close: close,
    invalidate: function () {
      cachedItems = null;
    },
  };
})();
