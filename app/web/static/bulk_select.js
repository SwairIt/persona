/* bulk_select.js — v0.65
 *
 * Bulk-select toolbar for timeline / search / favourites.
 *
 * Activation:
 *   Shift-click a thumbnail wrapper carrying `data-shot-id="<int>"`
 *   toggles its membership in the selection. The wrapper picks up a
 *   `data-bulk-selected` attribute + a ring outline so it's obvious
 *   which shots are currently selected. The wrapped `<a>` is a real
 *   link; we suppress its navigation when Shift is held so the click
 *   stays in the page.
 *
 * Toolbar (fixed, bottom of viewport, slides up when selection > 0):
 *   ┌────────────────────────────────────────────────────────────────┐
 *   │ N selected   [Add tag]  [Pin]  [Delete]  [Clear]               │
 *   └────────────────────────────────────────────────────────────────┘
 *
 *   - Add tag — `prompt()` for a tag name, then POST it to every
 *     selected shot via `/api/screenshot/{id}/tags` (v0.41 contract:
 *     form field `tag=<name>`, tag is auto-created if needed).
 *   - Pin    — POST each id to `/api/screenshots/{id}/pin`.
 *   - Delete — `confirm()` then POST each id to
 *              `/api/screenshot/{id}/delete` (recycle bin).
 *   - Clear  — drops every selection without touching the server.
 *
 * Persistence:
 *   The selection lives in localStorage under `persona.bulk_selection`
 *   as a JSON array of integer ids, so navigating between
 *   timeline ↔ search ↔ favourites keeps the same shots checked.
 *   Cross-tab updates are reflected via the `storage` event.
 *
 * DOM contract:
 *   [data-shot-id="<id>"]   — any element with this attribute is a
 *                             selectable target. Already present on
 *                             `_screenshot_card.html` wrappers.
 *   #bulk-toolbar           — placeholder div in base.html that we
 *                             populate on first activation.
 *
 * The script is idempotent — it re-binds on htmx swaps so newly
 * rendered cards still respond to Shift-clicks without a reload.
 */
(function () {
  "use strict";

  if (typeof document === "undefined" || typeof window === "undefined") {
    return;
  }

  var STORAGE_KEY = "persona.bulk_selection";
  var BOUND_ATTR = "data-bulk-bound";
  var SELECTED_ATTR = "data-bulk-selected";
  var MAX_SELECTION = 500;

  /** @returns {Set<number>} */
  function readSelection() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) {
        return new Set();
      }
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        return new Set();
      }
      var out = new Set();
      for (var i = 0; i < parsed.length; i += 1) {
        var n = Number(parsed[i]);
        if (Number.isFinite(n) && Number.isInteger(n) && n > 0) {
          out.add(n);
        }
      }
      return out;
    } catch (e) {
      return new Set();
    }
  }

  /** @param {Set<number>} sel */
  function writeSelection(sel) {
    try {
      var arr = Array.from(sel);
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(arr));
    } catch (e) {
      /* localStorage may be disabled or full — toolbar still works
         in-memory for this page, we just lose cross-page state. */
    }
  }

  function getCsrfToken() {
    // Persona's CSRF middleware exempts /api/* paths, but if the project
    // ever flips that flag we want one consistent place to plug a token
    // in. The middleware uses a `csrf_token` cookie by convention.
    var match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  /**
   * @param {string} url
   * @param {Record<string, string> | null} fields
   * @returns {Promise<Response>}
   */
  function postForm(url, fields) {
    var body = new URLSearchParams();
    if (fields) {
      Object.keys(fields).forEach(function (k) {
        body.append(k, fields[k]);
      });
    }
    var headers = {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-Requested-With": "fetch",
    };
    var token = getCsrfToken();
    if (token) {
      headers["X-CSRF-Token"] = token;
    }
    return fetch(url, {
      method: "POST",
      headers: headers,
      body: body.toString(),
      credentials: "same-origin",
    });
  }

  // ───────────────────────────── selection model ─────────────────────

  var state = {
    selection: readSelection(),
    /** @type {HTMLElement | null} */
    toolbar: null,
    /** @type {HTMLElement | null} */
    countEl: null,
    /** @type {HTMLButtonElement[]} */
    actionButtons: [],
    /** @type {HTMLElement | null} */
    statusEl: null,
    busy: false,
  };

  function getSelection() {
    return state.selection;
  }

  /** @param {number} id @param {boolean} on */
  function setSelected(id, on) {
    if (on) {
      if (state.selection.size >= MAX_SELECTION && !state.selection.has(id)) {
        setStatus(
          "Selection cap reached (" + MAX_SELECTION + ").",
          "warn"
        );
        return;
      }
      state.selection.add(id);
    } else {
      state.selection.delete(id);
    }
    writeSelection(state.selection);
    syncDom();
    renderToolbar();
  }

  function clearSelection() {
    state.selection = new Set();
    writeSelection(state.selection);
    syncDom();
    renderToolbar();
  }

  // ───────────────────────────── DOM sync ────────────────────────────

  function syncDom() {
    var nodes = document.querySelectorAll("[data-shot-id]");
    for (var i = 0; i < nodes.length; i += 1) {
      var el = nodes[i];
      var idAttr = el.getAttribute("data-shot-id");
      var id = Number(idAttr);
      if (!Number.isFinite(id) || id <= 0) {
        continue;
      }
      var on = state.selection.has(id);
      if (on) {
        el.setAttribute(SELECTED_ATTR, "1");
      } else {
        el.removeAttribute(SELECTED_ATTR);
      }
    }
  }

  function bindTargets(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll(
      "[data-shot-id]:not([" + BOUND_ATTR + "])"
    );
    for (var i = 0; i < nodes.length; i += 1) {
      var el = nodes[i];
      el.setAttribute(BOUND_ATTR, "1");
      el.addEventListener("click", onShotClick, true);
    }
  }

  /** @param {MouseEvent} ev */
  function onShotClick(ev) {
    if (!ev.shiftKey) {
      return;
    }
    var target = ev.currentTarget;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    var id = Number(target.getAttribute("data-shot-id"));
    if (!Number.isFinite(id) || id <= 0) {
      return;
    }
    // Stop the wrapping <a href> from navigating, and stop other
    // Shift-click handlers (e.g. range-select) from racing us.
    ev.preventDefault();
    ev.stopPropagation();
    var on = !state.selection.has(id);
    setSelected(id, on);
    // Suppress text selection that Shift-click would otherwise create.
    try {
      var sel = window.getSelection();
      if (sel) {
        sel.removeAllRanges();
      }
    } catch (e) {
      /* ignore */
    }
  }

  // ───────────────────────────── toolbar UI ──────────────────────────

  function ensureToolbar() {
    if (state.toolbar) {
      return state.toolbar;
    }
    var host = document.getElementById("bulk-toolbar");
    if (!host) {
      return null;
    }
    host.classList.add("persona-bulk-toolbar");
    host.setAttribute("role", "toolbar");
    host.setAttribute("aria-label", "Bulk-select actions");

    var inner = document.createElement("div");
    inner.className = "persona-bulk-toolbar__inner";

    var count = document.createElement("span");
    count.className = "persona-bulk-toolbar__count";
    count.textContent = "0 selected";
    inner.appendChild(count);

    var spacer = document.createElement("span");
    spacer.className = "persona-bulk-toolbar__spacer";
    inner.appendChild(spacer);

    var actions = [
      { label: "Add tag", title: "Tag every selected shot", handler: doAddTag },
      { label: "Pin", title: "Pin every selected shot", handler: doPin },
      {
        label: "Delete",
        title: "Move selected shots to the recycle bin",
        handler: doDelete,
        kind: "danger",
      },
      { label: "Clear", title: "Drop the selection", handler: doClear },
    ];

    var buttons = [];
    actions.forEach(function (spec) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className =
        "persona-bulk-toolbar__btn" +
        (spec.kind === "danger" ? " persona-bulk-toolbar__btn--danger" : "");
      btn.textContent = spec.label;
      btn.title = spec.title;
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        if (state.busy) {
          return;
        }
        spec.handler();
      });
      inner.appendChild(btn);
      buttons.push(btn);
    });

    var status = document.createElement("span");
    status.className = "persona-bulk-toolbar__status";
    status.setAttribute("aria-live", "polite");
    inner.appendChild(status);

    host.appendChild(inner);

    state.toolbar = host;
    state.countEl = count;
    state.actionButtons = buttons;
    state.statusEl = status;
    return host;
  }

  function renderToolbar() {
    var bar = ensureToolbar();
    if (!bar) {
      return;
    }
    var n = state.selection.size;
    if (state.countEl) {
      state.countEl.textContent = n + " selected";
    }
    if (n > 0) {
      bar.setAttribute("data-open", "1");
    } else {
      bar.removeAttribute("data-open");
      setStatus("", null);
    }
    var disabled = n === 0 || state.busy;
    state.actionButtons.forEach(function (btn) {
      // "Clear" should always work as long as selection > 0; the
      // network buttons share the same disabled rule.
      var isClear = btn.textContent === "Clear";
      btn.disabled = isClear ? n === 0 : disabled;
    });
  }

  /** @param {string} msg @param {"ok" | "warn" | "error" | null} kind */
  function setStatus(msg, kind) {
    if (!state.statusEl) {
      return;
    }
    state.statusEl.textContent = msg;
    state.statusEl.removeAttribute("data-kind");
    if (kind && msg) {
      state.statusEl.setAttribute("data-kind", kind);
    }
  }

  function setBusy(on) {
    state.busy = !!on;
    renderToolbar();
  }

  // ───────────────────────────── actions ─────────────────────────────

  /**
   * Fire `postForm` for every id, capped at MAX_SELECTION, and report
   * how many succeeded.
   *
   * @param {number[]} ids
   * @param {(id: number) => Promise<Response>} fn
   * @returns {Promise<{ok: number, fail: number}>}
   */
  async function runEach(ids, fn) {
    var ok = 0;
    var fail = 0;
    // Sequential keeps the server's per-shot work serialized and
    // avoids storming the DB with hundreds of parallel connections.
    for (var i = 0; i < ids.length; i += 1) {
      try {
        var resp = await fn(ids[i]);
        if (resp.ok) {
          ok += 1;
        } else {
          fail += 1;
        }
      } catch (e) {
        fail += 1;
      }
    }
    return { ok: ok, fail: fail };
  }

  function currentIds() {
    return Array.from(state.selection).slice(0, MAX_SELECTION);
  }

  async function doAddTag() {
    var ids = currentIds();
    if (!ids.length) {
      return;
    }
    var raw = window.prompt("Tag name to apply to " + ids.length + " shot(s):");
    if (raw === null) {
      return;
    }
    var name = raw.trim();
    if (!name) {
      setStatus("Tag name was empty.", "warn");
      return;
    }
    setBusy(true);
    setStatus("Tagging " + ids.length + "…", null);
    var res = await runEach(ids, function (id) {
      return postForm(
        "/api/screenshot/" + encodeURIComponent(id) + "/tags",
        { tag: name }
      );
    });
    setBusy(false);
    finishStatus("Tagged", res, name);
    document.dispatchEvent(
      new CustomEvent("persona:tag-applied", {
        detail: { source: "bulk_select", tag: name, ids: ids },
      })
    );
  }

  async function doPin() {
    var ids = currentIds();
    if (!ids.length) {
      return;
    }
    setBusy(true);
    setStatus("Pinning " + ids.length + "…", null);
    var res = await runEach(ids, function (id) {
      return postForm("/api/screenshots/" + encodeURIComponent(id) + "/pin", null);
    });
    setBusy(false);
    finishStatus("Pinned", res, null);
  }

  async function doDelete() {
    var ids = currentIds();
    if (!ids.length) {
      return;
    }
    var ok = window.confirm(
      "Move " + ids.length + " shot(s) to the recycle bin?"
    );
    if (!ok) {
      return;
    }
    setBusy(true);
    setStatus("Deleting " + ids.length + "…", null);
    var res = await runEach(ids, function (id) {
      return postForm(
        "/api/screenshot/" + encodeURIComponent(id) + "/delete",
        null
      );
    });
    setBusy(false);
    // Drop successfully-deleted ids from the selection so the user
    // doesn't accidentally re-trigger an op on stale rows. We can't
    // tell which individual id failed without per-id bookkeeping,
    // so on a full success we wipe the whole set; on partial failure
    // we keep the set so the user can retry.
    if (res.fail === 0) {
      clearSelection();
    }
    finishStatus("Deleted", res, null);
  }

  function doClear() {
    if (state.selection.size === 0) {
      return;
    }
    clearSelection();
    setStatus("Selection cleared.", "ok");
  }

  /**
   * @param {string} verb
   * @param {{ok: number, fail: number}} res
   * @param {string | null} subject
   */
  function finishStatus(verb, res, subject) {
    var detail = res.ok + " ok";
    if (res.fail) {
      detail += ", " + res.fail + " failed";
    }
    if (subject) {
      detail += " · " + subject;
    }
    setStatus(verb + ": " + detail, res.fail ? "error" : "ok");
  }

  // ───────────────────────────── boot ────────────────────────────────

  function init() {
    ensureToolbar();
    bindTargets(document);
    syncDom();
    renderToolbar();
  }

  // Re-bind after htmx swaps so freshly-loaded cards work too.
  document.addEventListener("htmx:afterSwap", function (ev) {
    var root = ev && ev.detail && ev.detail.target;
    bindTargets(root instanceof HTMLElement ? root : document);
    syncDom();
  });

  // Cross-tab sync: another tab edited the selection.
  window.addEventListener("storage", function (ev) {
    if (ev.key !== STORAGE_KEY) {
      return;
    }
    state.selection = readSelection();
    syncDom();
    renderToolbar();
  });

  // Quality-of-life: ESC clears the selection when the toolbar is open
  // and no input is focused.
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") {
      return;
    }
    if (state.selection.size === 0) {
      return;
    }
    var t = ev.target;
    if (t instanceof HTMLElement) {
      var tag = t.tagName;
      if (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        t.isContentEditable
      ) {
        return;
      }
    }
    clearSelection();
    setStatus("Selection cleared.", "ok");
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
