/*
 * Persona — палитра настроек (Ctrl+Shift+P / Cmd+Shift+P).
 *
 * Отдельная от навигационной палитры (Ctrl+K, см. command_palette.js):
 * та прыгает по страницам-роутам, эта ищет по ВСЕМ настройкам через
 * серверный /api/settings/search (источник правды — _CATEGORIES в
 * settings_hub.py). Стиль — как VS Code Command Palette: overlay сверху,
 * инпут, список результатов с категорией, ↑↓ навигация, Enter — переход,
 * ESC — закрыть.
 *
 * Vanilla ES2020, без фреймворков. Tailwind-классы те же, что в base.html
 * (ink-*, accent-*, zinc-*). Переводимые строки приходят из base.html через
 * глобал window.PERSONA_SETTINGS_PALETTE_I18N (t('...') в шаблоне), с
 * безопасными англ. дефолтами на случай его отсутствия.
 */
(function () {
  "use strict";

  var SEARCH_ENDPOINT = "/api/settings/search";
  // Слайс D1 — ИИ-поиск по настройкам. Дёргаем ТОЛЬКО когда обычный keyword-
  // поиск дал 0 результатов. При 404 (мастер-режим «ИИ везде» выключен) молча
  // остаёмся на обычной палитре — фича как будто не существует.
  var AI_SEARCH_ENDPOINT = "/api/settings/ai-search";
  // Раз получив 404 от ai-search, больше не долбим его в этой сессии страницы.
  var aiSearchDisabled = false;

  // Переводимые строки: base.html кладёт их в глобал через t(...). Дефолты —
  // на случай, если скрипт подключили без конфига (другой шаблон/тест).
  var I18N = (window.PERSONA_SETTINGS_PALETTE_I18N || {});
  function L(key, fallback) {
    return (I18N && typeof I18N[key] === "string" && I18N[key]) || fallback;
  }

  var overlayEl = null;
  var inputEl = null;
  var listEl = null;
  var hintEl = null;
  var results = [];
  var focusIdx = 0;
  var aiUsed = false; // последняя выдача пришла от ИИ-поиска (для пометки «✨»)
  var reqSeq = 0; // защита от гонки ответов /search (показываем только последний)
  var debounceTimer = null;

  // ---------------------------------------------------------------------
  // Поиск
  // ---------------------------------------------------------------------

  function runSearch() {
    if (!inputEl || !listEl) return;
    var q = inputEl.value.trim();
    if (!q) {
      results = [];
      focusIdx = 0;
      aiUsed = false;
      renderEmpty(L("hint_type", "Type to search settings…"));
      return;
    }
    var mySeq = ++reqSeq;
    fetch(SEARCH_ENDPOINT + "?q=" + encodeURIComponent(q), {
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.ok ? r.json() : { results: [] };
      })
      .then(function (j) {
        if (mySeq !== reqSeq) return; // пришёл устаревший ответ — игнор
        results = Array.isArray(j && j.results) ? j.results : [];
        focusIdx = 0;
        aiUsed = false;
        if (results.length === 0) {
          // Обычный поиск пуст — пробуем ИИ (если «ИИ везде» включён).
          tryAiSearch(q, mySeq);
          return;
        }
        render();
      })
      .catch(function () {
        if (mySeq !== reqSeq) return;
        results = [];
        aiUsed = false;
        renderEmpty(L("no_matches", "No matches."));
      });
  }

  // ---------------------------------------------------------------------
  // ИИ-поиск (слайс D1) — фоллбэк, когда keyword-поиск дал 0.
  //
  // POST /api/settings/ai-search {intent}. 404 → фича выключена, тихо
  // остаёмся на «ничего не найдено» и больше не дёргаем ИИ. Иначе —
  // показываем ИИ-результаты с пометкой «✨ ИИ нашёл».
  // ---------------------------------------------------------------------

  function tryAiSearch(q, mySeq) {
    if (aiSearchDisabled) {
      renderEmpty(L("no_matches", "No matches."));
      return;
    }
    // Показать «ИИ ищёт…», чтобы UI не выглядел зависшим (LLM на ПК не мгновенен).
    renderEmpty(L("ai_searching", "✨ AI is searching…"));
    fetch(AI_SEARCH_ENDPOINT, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intent: q }),
    })
      .then(function (r) {
        if (r.status === 404) {
          aiSearchDisabled = true; // «ИИ везде» выкл — не пробуем повторно
          return null;
        }
        return r.ok ? r.json() : null;
      })
      .then(function (j) {
        if (mySeq !== reqSeq) return; // устаревший ответ — игнор
        if (!j) {
          renderEmpty(L("no_matches", "No matches."));
          return;
        }
        results = Array.isArray(j.results) ? j.results : [];
        aiUsed = !!j.ai_used;
        focusIdx = 0;
        render();
      })
      .catch(function () {
        if (mySeq !== reqSeq) return;
        renderEmpty(L("no_matches", "No matches."));
      });
  }

  function scheduleSearch() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(runSearch, 120);
  }

  // ---------------------------------------------------------------------
  // Рендер
  // ---------------------------------------------------------------------

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function renderEmpty(message) {
    if (!listEl) return;
    listEl.innerHTML =
      '<div class="px-4 py-6 text-center text-sm text-zinc-500">' +
      escapeHtml(message) +
      "</div>";
  }

  function render() {
    if (!listEl) return;
    if (results.length === 0) {
      renderEmpty(L("no_matches", "No matches."));
      return;
    }
    if (focusIdx >= results.length) focusIdx = 0;
    // Слайс D1 — плашка «✨ ИИ нашёл» над результатами, если это ИИ-выдача.
    var aiBanner = aiUsed
      ? '<div class="px-4 py-1.5 mb-1 text-xs font-medium text-accent-300">' +
        escapeHtml(L("ai_found", "✨ AI found")) +
        "</div>"
      : "";
    var rows = results.map(function (item, idx) {
      var active = idx === focusIdx;
      var cls = active
        ? "bg-accent-600/30 text-accent-100"
        : "text-zinc-300 hover:bg-ink-800";
      var icon = item.icon
        ? '<span class="mr-2 text-base leading-none">' +
          escapeHtml(item.icon) +
          "</span>"
        : "";
      return (
        '<a href="' +
        escapeHtml(item.href) +
        '" data-idx="' +
        idx +
        '" class="settings-palette-item flex items-center justify-between gap-3 px-4 py-2 rounded ' +
        cls +
        '">' +
        '<span class="flex items-center min-w-0">' +
        icon +
        '<span class="truncate font-medium">' +
        escapeHtml(item.label) +
        "</span>" +
        "</span>" +
        '<span class="flex items-center gap-2 shrink-0 text-xs text-zinc-500">' +
        '<span class="hidden sm:inline">' +
        escapeHtml(item.category || "") +
        "</span>" +
        '<span class="font-mono">' +
        escapeHtml(item.href) +
        "</span>" +
        "</span>" +
        "</a>"
      );
    });
    listEl.innerHTML = aiBanner + rows.join("");
    var activeRow = listEl.querySelector('[data-idx="' + focusIdx + '"]');
    if (activeRow && typeof activeRow.scrollIntoView === "function") {
      activeRow.scrollIntoView({ block: "nearest" });
    }
  }

  // ---------------------------------------------------------------------
  // Открыть / закрыть
  // ---------------------------------------------------------------------

  function ensureRoot() {
    var root = document.getElementById("palette-root");
    if (!root) {
      root = document.createElement("div");
      root.id = "palette-root";
      document.body.appendChild(root);
    }
    return root;
  }

  function open() {
    if (overlayEl) return;
    // Если открыта навигационная палитра (Ctrl+K) — не наслаиваемся.
    if (
      window.PersonaPalette &&
      document.querySelector(".persona-palette-overlay")
    ) {
      try {
        window.PersonaPalette.close();
      } catch (e) {
        /* ignore */
      }
    }
    var root = ensureRoot();
    overlayEl = document.createElement("div");
    overlayEl.className =
      "persona-settings-palette-overlay fixed inset-0 z-[61] bg-black/50 flex items-start justify-center pt-24 px-4";
    overlayEl.innerHTML =
      '<div class="bg-ink-900 border border-ink-700 rounded-lg shadow-2xl w-full max-w-2xl overflow-hidden">' +
      '  <input type="text" id="persona-settings-palette-input" autocomplete="off" spellcheck="false"' +
      '         placeholder="' +
      escapeHtml(L("placeholder", "Search settings…  (type to filter, ↑↓ Enter Esc)")) +
      '"' +
      '         class="w-full px-4 py-3 bg-transparent border-0 border-b border-ink-700 text-zinc-100 focus:outline-none">' +
      '  <div id="persona-settings-palette-list" class="max-h-[60vh] overflow-y-auto p-2 text-sm"></div>' +
      '  <div class="px-4 py-2 border-t border-ink-700 flex items-center justify-between text-xs text-zinc-500">' +
      '    <span>' +
      escapeHtml(L("footer_hint", "↑↓ navigate · Enter open · Esc close")) +
      "</span>" +
      '    <span class="font-mono">Ctrl / Cmd + Shift + P</span>' +
      "  </div>" +
      "</div>";

    overlayEl.addEventListener("click", function (e) {
      if (e.target === overlayEl) close();
    });

    root.appendChild(overlayEl);
    inputEl = document.getElementById("persona-settings-palette-input");
    listEl = document.getElementById("persona-settings-palette-list");
    hintEl = null;
    focusIdx = 0;
    results = [];

    inputEl.addEventListener("input", scheduleSearch);
    inputEl.addEventListener("keydown", handleKey);
    listEl.addEventListener("click", function (e) {
      var a = e.target && e.target.closest && e.target.closest(".settings-palette-item");
      if (!a) return;
      // браузер сам перейдёт по href — отдельный pushState не нужен.
    });

    renderEmpty(L("hint_type", "Type to search settings…"));
    inputEl.focus();
  }

  function close() {
    if (debounceTimer) {
      clearTimeout(debounceTimer);
      debounceTimer = null;
    }
    if (!overlayEl) return;
    overlayEl.remove();
    overlayEl = null;
    inputEl = null;
    listEl = null;
    results = [];
    focusIdx = 0;
    aiUsed = false;
  }

  function handleKey(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (results.length) {
        focusIdx = Math.min(results.length - 1, focusIdx + 1);
        render();
      }
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (results.length) {
        focusIdx = Math.max(0, focusIdx - 1);
        render();
      }
    } else if (e.key === "Home") {
      e.preventDefault();
      focusIdx = 0;
      render();
    } else if (e.key === "End") {
      e.preventDefault();
      focusIdx = Math.max(0, results.length - 1);
      render();
    } else if (e.key === "Enter") {
      e.preventDefault();
      var target = results[focusIdx];
      if (target && target.href) {
        window.location.href = target.href;
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }

  // ---------------------------------------------------------------------
  // Глобальный хоткей — Ctrl+Shift+P / Cmd+Shift+P.
  //
  // ВАЖНО: НЕ конфликтуем с Ctrl+K (навигационная палитра). Требуем именно
  // Shift+P; некоторые браузеры на Ctrl+Shift отдают key === "P" (верхний
  // регистр), поэтому матчим оба регистра.
  // ---------------------------------------------------------------------

  document.addEventListener("keydown", function (e) {
    if (
      (e.ctrlKey || e.metaKey) &&
      e.shiftKey &&
      (e.key === "p" || e.key === "P")
    ) {
      // Ctrl+Shift+P в Firefox = приватное окно; отменяем дефолт, чтобы
      // вместо этого открыть нашу палитру внутри вкладки.
      e.preventDefault();
      if (overlayEl) {
        close();
      } else {
        open();
      }
    }
  });

  // Маленький императивный API — для тестов / dev-консоли.
  window.PersonaSettingsPalette = { open: open, close: close };
})();
