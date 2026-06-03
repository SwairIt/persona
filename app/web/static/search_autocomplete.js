/* Search query autocomplete — vanilla JS, no framework.
 *
 * Looks up a single input matched by `#search-input` or `[data-autocomplete]`,
 * debounces input by 200ms, hits /api/search/autocomplete?q=... and renders a
 * dropdown anchored beneath the input. Keyboard:
 *
 *   ArrowDown / ArrowUp — move highlight
 *   Enter               — pick highlighted (or first) suggestion
 *   Escape              — close the dropdown
 *
 * No external dependencies. The dropdown is positioned absolutely inside a
 * wrapper that is inserted as the input's parent so it tracks the input even
 * when the page layout shifts.
 */
(function () {
  "use strict";

  var DEBOUNCE_MS = 200;
  var API = "/api/search/autocomplete";
  var MAX_RENDER = 8;

  function findInput() {
    var byId = document.getElementById("search-input");
    if (byId) return byId;
    return document.querySelector("[data-autocomplete]");
  }

  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments;
      var self = this;
      if (t !== null) {
        clearTimeout(t);
      }
      t = setTimeout(function () {
        t = null;
        fn.apply(self, args);
      }, ms);
    };
  }

  function attach(input) {
    // Wrap the input so the dropdown can be positioned relative to it without
    // disturbing the surrounding flex layout on /search.
    var wrapper = document.createElement("span");
    wrapper.className = "persona-autocomplete-wrapper";
    wrapper.style.position = "relative";
    wrapper.style.display = "block";
    wrapper.style.flex = "1";

    var parent = input.parentNode;
    if (!parent) return;
    parent.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    var dropdown = document.createElement("ul");
    dropdown.className = "persona-autocomplete-dropdown";
    dropdown.setAttribute("role", "listbox");
    dropdown.style.position = "absolute";
    dropdown.style.left = "0";
    dropdown.style.right = "0";
    dropdown.style.top = "100%";
    dropdown.style.marginTop = "4px";
    dropdown.style.zIndex = "50";
    dropdown.style.background = "#1f1f23";
    dropdown.style.border = "1px solid #3f3f46";
    dropdown.style.borderRadius = "0.5rem";
    dropdown.style.padding = "4px";
    dropdown.style.listStyle = "none";
    dropdown.style.maxHeight = "320px";
    dropdown.style.overflowY = "auto";
    dropdown.style.display = "none";
    dropdown.style.boxShadow = "0 8px 24px rgba(0,0,0,0.4)";
    wrapper.appendChild(dropdown);

    var state = {
      items: [],
      activeIdx: -1,
      lastQuery: "",
      reqSeq: 0,
    };

    function close() {
      dropdown.style.display = "none";
      state.items = [];
      state.activeIdx = -1;
    }

    function render() {
      dropdown.innerHTML = "";
      if (state.items.length === 0) {
        dropdown.style.display = "none";
        return;
      }
      for (var i = 0; i < state.items.length && i < MAX_RENDER; i++) {
        var s = state.items[i];
        var li = document.createElement("li");
        li.setAttribute("role", "option");
        li.dataset.idx = String(i);
        li.style.padding = "6px 10px";
        li.style.cursor = "pointer";
        li.style.borderRadius = "0.375rem";
        li.style.display = "flex";
        li.style.alignItems = "center";
        li.style.justifyContent = "space-between";
        li.style.gap = "8px";
        li.style.fontFamily = "ui-monospace, SFMono-Regular, Menlo, monospace";
        li.style.fontSize = "13px";
        li.style.color = "#e4e4e7";

        var label = document.createElement("span");
        label.textContent = s.text;
        label.style.overflow = "hidden";
        label.style.textOverflow = "ellipsis";
        label.style.whiteSpace = "nowrap";
        li.appendChild(label);

        var badge = document.createElement("span");
        badge.textContent = s.kind;
        badge.style.fontSize = "10px";
        badge.style.textTransform = "uppercase";
        badge.style.letterSpacing = "0.05em";
        badge.style.color = s.kind === "saved" ? "#a78bfa" : "#71717a";
        badge.style.flexShrink = "0";
        li.appendChild(badge);

        if (i === state.activeIdx) {
          li.style.background = "#3f3f46";
        }

        li.addEventListener("mouseenter", makeHoverHandler(i));
        li.addEventListener("mousedown", makePickHandler(i));
        dropdown.appendChild(li);
      }
      dropdown.style.display = "block";
    }

    function makeHoverHandler(idx) {
      return function () {
        state.activeIdx = idx;
        render();
      };
    }

    function makePickHandler(idx) {
      return function (ev) {
        // mousedown rather than click so the input doesn't lose focus before
        // we can read the value back.
        ev.preventDefault();
        pick(idx);
      };
    }

    function pick(idx) {
      var s = state.items[idx];
      if (!s) return;
      input.value = s.text;
      close();
      // Fire an input event so any HTMX / Alpine listeners on the page
      // (the /search form uses ``keyup changed`` to refresh results) notice.
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    }

    function fetchSuggestions(q) {
      state.reqSeq += 1;
      var mine = state.reqSeq;
      var url = API + "?q=" + encodeURIComponent(q);
      fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) throw new Error("autocomplete http " + r.status);
          return r.json();
        })
        .then(function (data) {
          if (mine !== state.reqSeq) return; // a newer request superseded us
          var list = (data && data.suggestions) || [];
          state.items = list.slice(0, MAX_RENDER);
          state.activeIdx = state.items.length > 0 ? 0 : -1;
          render();
        })
        .catch(function () {
          // Network / parse error: drop silently — the form still works.
          if (mine !== state.reqSeq) return;
          close();
        });
    }

    var debouncedFetch = debounce(function (q) {
      fetchSuggestions(q);
    }, DEBOUNCE_MS);

    input.setAttribute("autocomplete", "off");
    input.addEventListener("input", function () {
      var q = input.value.trim();
      state.lastQuery = q;
      if (q.length === 0) {
        close();
        return;
      }
      debouncedFetch(q);
    });

    input.addEventListener("keydown", function (ev) {
      if (dropdown.style.display === "none") return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        if (state.items.length === 0) return;
        state.activeIdx = (state.activeIdx + 1) % state.items.length;
        render();
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        if (state.items.length === 0) return;
        state.activeIdx =
          (state.activeIdx - 1 + state.items.length) % state.items.length;
        render();
      } else if (ev.key === "Enter") {
        if (state.activeIdx >= 0 && state.items[state.activeIdx]) {
          ev.preventDefault();
          pick(state.activeIdx);
        }
      } else if (ev.key === "Escape") {
        ev.preventDefault();
        close();
      }
    });

    input.addEventListener("blur", function () {
      // Small delay so a mousedown on a suggestion still registers.
      setTimeout(close, 120);
    });
  }

  function boot() {
    var input = findInput();
    if (!input) return;
    if (input.dataset.autocompleteBound === "1") return;
    input.dataset.autocompleteBound = "1";
    attach(input);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
