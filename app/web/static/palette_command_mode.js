/*
 * Persona — colon-command mode for the Cmd+K palette.
 *
 * Loaded as a separate file (NOT a patch to command_palette.js) so the
 * fuzzy-search code stays intact. Activation is opt-in: include this
 * script from a page (or paste into DevTools) and it attaches itself to
 * the existing palette as soon as window.PersonaPalette is available.
 *
 * Behaviour:
 *   - When the palette input value starts with ":", a hint banner appears
 *     above the result list showing the matched command syntax + help.
 *   - Pressing Enter while in command mode POSTs to /api/palette/command
 *     instead of navigating to the highlighted item.
 *   - On a successful response with a `redirect` field, the browser
 *     navigates there (used by :goto).
 *   - On any other success, the palette closes and a tiny toast appears
 *     with the server message.
 *
 * No external dependencies. Vanilla ES2020. Tailwind classes mirror
 * base.html (ink-*, accent-*, zinc-*).
 */
(function () {
  "use strict";

  const CATALOGUE_ENDPOINT = "/api/palette/commands.json";
  const EXEC_ENDPOINT = "/api/palette/command";

  /** @typedef {{name: string, syntax: string, description: string, handler_url: string}} CommandSpec */

  /** @type {CommandSpec[] | null} */
  let catalogue = null;
  /** @type {Promise<CommandSpec[]> | null} */
  let pendingCatalogue = null;

  function loadCatalogue() {
    if (catalogue) return Promise.resolve(catalogue);
    if (pendingCatalogue) return pendingCatalogue;
    pendingCatalogue = fetch(CATALOGUE_ENDPOINT, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : { commands: [] }))
      .then((j) => {
        catalogue = Array.isArray(j && j.commands) ? j.commands : [];
        return catalogue;
      })
      .catch(() => {
        catalogue = [];
        return catalogue;
      });
    return pendingCatalogue;
  }

  function matchCommand(raw) {
    if (!catalogue || !raw || raw[0] !== ":") return null;
    const head = raw.slice(1).split(/\s+/, 1)[0].toLowerCase();
    if (!head) return null;
    // Prefer exact match, fall back to prefix match.
    let exact = null;
    let prefix = null;
    for (const spec of catalogue) {
      if (spec.name === head) {
        exact = spec;
        break;
      }
      if (!prefix && spec.name.startsWith(head)) prefix = spec;
    }
    return exact || prefix;
  }

  function ensureBanner(overlay) {
    let banner = overlay.querySelector("[data-persona-cmd-banner]");
    if (banner) return banner;
    banner = document.createElement("div");
    banner.setAttribute("data-persona-cmd-banner", "1");
    banner.className =
      "mx-3 mb-2 px-3 py-2 bg-ink-800 border border-accent-500 rounded " +
      "text-xs text-zinc-300 font-mono";
    const input = overlay.querySelector("input");
    if (input && input.parentElement) {
      input.parentElement.insertAdjacentElement("afterend", banner);
    } else {
      overlay.prepend(banner);
    }
    return banner;
  }

  function clearBanner(overlay) {
    const banner = overlay.querySelector("[data-persona-cmd-banner]");
    if (banner && banner.parentElement) banner.parentElement.removeChild(banner);
  }

  function renderBanner(overlay, raw) {
    const spec = matchCommand(raw);
    const banner = ensureBanner(overlay);
    if (!spec) {
      banner.textContent =
        "unknown command — type :help to list every colon-command";
      banner.classList.add("text-red-400");
      return;
    }
    banner.classList.remove("text-red-400");
    banner.innerHTML = "";
    const syntax = document.createElement("div");
    syntax.className = "text-accent-300";
    syntax.textContent = spec.syntax;
    const desc = document.createElement("div");
    desc.className = "text-zinc-400 mt-0.5";
    desc.textContent = spec.description;
    banner.appendChild(syntax);
    banner.appendChild(desc);
  }

  function toast(message, ok) {
    const node = document.createElement("div");
    node.textContent = message || (ok ? "ok" : "error");
    node.className =
      "fixed bottom-6 right-6 z-[10000] px-4 py-2 rounded shadow-lg " +
      "text-sm font-mono " +
      (ok
        ? "bg-accent-600 text-white"
        : "bg-red-600 text-white");
    document.body.appendChild(node);
    window.setTimeout(() => {
      if (node.parentElement) node.parentElement.removeChild(node);
    }, 3500);
  }

  function execute(raw, overlay) {
    fetch(EXEC_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: raw }),
    })
      .then((r) => r.json().catch(() => ({ ok: false, message: "bad response" })))
      .then((j) => {
        const ok = Boolean(j && j.ok);
        if (ok && j && j.redirect) {
          window.location.href = j.redirect;
          return;
        }
        toast((j && j.message) || (ok ? "done" : "failed"), ok);
        if (ok && window.PersonaPalette && typeof window.PersonaPalette.close === "function") {
          window.PersonaPalette.close();
        }
      })
      .catch(() => toast("network error", false));
  }

  function attach() {
    const overlay = document.querySelector("[data-persona-palette]")
      || document.getElementById("persona-palette-overlay")
      || document.querySelector(".persona-palette");
    if (!overlay) return false;
    const input = overlay.querySelector("input");
    if (!input || input.dataset.personaCmdAttached === "1") return Boolean(input);
    input.dataset.personaCmdAttached = "1";

    input.addEventListener("input", () => {
      const raw = input.value || "";
      if (raw.startsWith(":")) {
        loadCatalogue().then(() => renderBanner(overlay, raw));
      } else {
        clearBanner(overlay);
      }
    });

    input.addEventListener(
      "keydown",
      (e) => {
        if (e.key !== "Enter") return;
        const raw = (input.value || "").trim();
        if (!raw.startsWith(":")) return;
        // Pre-empt the fuzzy-search handler.
        e.preventDefault();
        e.stopPropagation();
        execute(raw, overlay);
      },
      true, // capture-phase so we beat the search handler
    );
    return true;
  }

  // The palette overlay is created lazily on first Cmd+K. Poll until
  // it exists, then attach exactly once. Stops polling after 60 s to
  // avoid leaking timers on pages that never open the palette.
  let attempts = 0;
  const MAX_ATTEMPTS = 120; // 60 s at 500 ms cadence
  const handle = window.setInterval(() => {
    attempts += 1;
    if (attach() || attempts >= MAX_ATTEMPTS) {
      window.clearInterval(handle);
    }
  }, 500);

  // Also try once synchronously in case the overlay is already there.
  attach();

  // Expose a tiny imperative API for tests / dev console.
  window.PersonaPaletteCommandMode = {
    catalogue: loadCatalogue,
    match: matchCommand,
    execute: execute,
    attach: attach,
  };
})();
