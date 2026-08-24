/*
 * Persona — вездесущий копилот («ИИ везде», слайс B1), клиент.
 *
 * Alpine-компонент copilotWidget(): плавающая кнопка → выезжающая панель.
 * Поле «спроси что угодно» + быстрые режимы (💬 спросить, 📄 саммари
 * страницы, 🔎 найти настройку). Отправка открывает SSE-стрим
 * /api/copilot/ask (эндпоинт — слайс B2) и рендерит delta в пузырь.
 *
 * Контракт SSE (совпадает с /api/ask/stream и qa_stream):
 *   {type:"meta", ...}
 *   {type:"delta", text:"..."}
 *   {type:"done",  full_answer:"...", ...}
 *   {type:"error", reason:"disabled"|"llm_not_configured"|"missing_config"|
 *                         "llm_offline"|"bad_request"|"internal",
 *                  detail?:"...", href?:"/settings/llm"}
 *
 * "disabled" бывает ТОЛЬКО у владельца (его мастер-флаг «ИИ везде»);
 * участнику без своей модели прилетает "llm_not_configured" со ссылкой на
 * /settings/llm — раньше ему показывали «ИИ везде выключен» и вели на
 * owner-only страницу, которую он не может открыть.
 * Для find_setting ответ может нести ссылку — показываем кликабельно.
 *
 * Зависимостей нет (Alpine уже подключён в base.html). Переводимые строки
 * приходят из window.PERSONA_COPILOT_I18N (см. inline-словарь в base.html).
 */

function copilotWidget() {
  const I18N = (window.PERSONA_COPILOT_I18N || {});
  const t = (key, fallback) => (I18N[key] != null ? I18N[key] : (fallback || key));

  return {
    open: false,
    q: "",
    mode: "ask", // ask | summary | find_setting
    busy: false,
    // messages: [{role:'user'|'bot'|'error', text, streaming?, href?, hrefLabel?}]
    messages: [],
    _es: null, // активный EventSource
    _gotDelta: false,

    init() {
      // Публичный мини-API: палитра Cmd+K умеет отправить сюда свободный
      // вопрос («спросить помощника»), не заводя второй чат-виджет.
      window.PersonaCopilot = {
        open: () => this.openPanel(),
        ask: (text) => {
          this.mode = "ask";
          const q = (text || "").trim();
          if (q) this.send(q);
          else this.openPanel();
        },
      };

      // Хоткей Ctrl+/ (и Cmd+/ на mac) — открыть панель и сфокусировать поле.
      document.addEventListener("keydown", (e) => {
        const combo = (e.ctrlKey || e.metaKey) && e.key === "/";
        if (combo) {
          e.preventDefault();
          this.openPanel();
        }
        if (e.key === "Escape" && this.open) {
          this.closePanel();
        }
      });
    },

    openPanel() {
      this.open = true;
      // Фокус в поле на следующем тике, когда панель уже в DOM-потоке.
      this.$nextTick(() => {
        const input = this.$refs.input;
        if (input) input.focus();
      });
    },

    closePanel() {
      this.open = false;
      // Стрим не рвём — пусть текущий ответ дойдёт; юзер может открыть снова.
    },

    toggle() {
      if (this.open) this.closePanel();
      else this.openPanel();
    },

    // Быстрый режим: клик по чипу выставляет режим и (для явных действий)
    // сразу отправляет запрос без ввода текста.
    quick(mode) {
      this.mode = mode;
      if (mode === "summary") {
        this.send(t("summary_request", "Кратко перескажи эту страницу."));
      } else {
        // ask / find_setting — просто фокус в поле, юзер вводит запрос.
        this.openPanel();
      }
    },

    submit() {
      const text = (this.q || "").trim();
      if (!text) return;
      this.send(text);
      this.q = "";
    },

    send(text) {
      if (this.busy) return; // не плодим параллельные стримы
      this.openPanel();

      // Для авто-режимов (саммари страницы) не всегда есть видимый ввод —
      // показываем понятную реплику пользователя всё равно.
      this.messages.push({ role: "user", text: text });

      const bot = {
        role: "bot",
        text: "",
        streaming: true,
        href: "",
        hrefLabel: "",
        href2: "",
        hrefLabel2: "",
      };
      this.messages.push(bot);
      this.busy = true;
      this._gotDelta = false;
      this.$nextTick(() => this._scrollToEnd());

      const params = new URLSearchParams({
        q: text,
        page_url: location.pathname + location.search,
        mode: this.mode,
      });
      const url = "/api/copilot/ask?" + params.toString();

      let es;
      try {
        es = new EventSource(url);
      } catch (err) {
        this._fail(bot, t("err_generic", "Не удалось открыть соединение."));
        return;
      }
      this._es = es;

      es.onmessage = (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch (e) {
          return; // мусорный кадр — игнор
        }
        this._handleEvent(data, bot);
      };

      es.onerror = () => {
        // EventSource сам ретраит; но если ответ ещё не начался и соединение
        // упало — это, скорее всего, offline/недоступность. Закрываем.
        es.close();
        if (this._es === es) this._es = null;
        if (bot.streaming && !this._gotDelta) {
          this._fail(bot, t("err_offline", "ИИ сейчас недоступен. Попробуйте позже."));
        } else {
          // ответ уже частично пришёл — просто финализируем то, что есть
          this._finalize(bot);
        }
      };
    },

    _handleEvent(data, bot) {
      const type = data && data.type;
      if (type === "delta") {
        this._gotDelta = true;
        bot.text += (data.text || "");
        this.$nextTick(() => this._scrollToEnd());
        return;
      }
      if (type === "meta") {
        return; // служебное — ничего не рисуем
      }
      if (type === "done") {
        // done может нести полный ответ и (для find_setting) ссылку.
        if (!this._gotDelta && data.full_answer) {
          bot.text = data.full_answer;
        }
        this._applyLink(data, bot);
        this._closeStream();
        this._finalize(bot);
        return;
      }
      if (type === "error") {
        this._closeStream();
        this._fail(bot, this._errorInfo(data));
        return;
      }
    },

    // Пытаемся достать ссылку на настройку из события. Поддерживаем и
    // явные поля (url/href + label/title), и одиночный элемент settings[].
    _applyLink(data, bot) {
      let href = data.url || data.href || "";
      let label = data.label || data.title || "";
      if (!href && Array.isArray(data.settings) && data.settings.length) {
        const s = data.settings[0] || {};
        href = s.url || s.href || s.path || "";
        label = s.label || s.title || s.name || href;
      }
      if (href) {
        bot.href = href;
        bot.hrefLabel = label || href;
      }
    },

    // Причина отказа → что показать. Возвращаем объект, а не строку: у части
    // причин есть действие (ссылка), и без него сообщение — тупик.
    //
    // Две причины намеренно РАЗВЕДЕНЫ и не путаются:
    //   * "disabled"           — владелец выключил свой мастер-флаг «ИИ везде»;
    //                            чинится на /settings/ai-everywhere (owner-only).
    //   * "llm_not_configured" — у участника нет СВОЕЙ модели; страница
    //                            /settings/ai-everywhere ему недоступна и к делу
    //                            не относится, ведём на /settings/llm.
    _errorInfo(data) {
      const reason = (data && data.reason) || "";
      const detail = data && data.detail;
      switch (reason) {
        case "disabled":
          return {
            text: t("err_disabled", "Режим «ИИ везде» выключен."),
            href: "/settings/ai-everywhere",
            hrefLabel: t("err_disabled_link", "Включить «ИИ везде»"),
          };
        case "missing_config":
        case "llm_offline":
        case "not_configured":
          return { text: t("err_offline", "ИИ сейчас недоступен. Попробуйте позже.") };
        case "llm_not_configured":
          // У пользователя нет СВОЕГО провайдера — это чинится настройкой,
          // а не ожиданием, поэтому ведём на страницу выбора провайдера.
          return {
            text: t(
              "err_not_configured",
              "Подключи свою модель — и помощник заработает на всём сайте."
            ),
            href: (data && data.href) || "/settings/llm",
            hrefLabel: t("err_not_configured_link", "Подключить модель → /settings/llm"),
            href2: "/help/connect-llm",
            hrefLabel2: t("err_not_configured_help", "как получить ключ бесплатно"),
          };
        case "bad_request":
          return { text: t("err_empty", "Пустой запрос.") };
        default:
          return {
            text: detail
              ? t("err_generic", "Что-то пошло не так.") + " (" + detail + ")"
              : t("err_generic", "Что-то пошло не так."),
          };
      }
    },

    _fail(bot, info) {
      // info — строка (внутренние вызовы) или объект {text, href, ...}.
      const data = typeof info === "string" ? { text: info } : (info || {});
      // Превращаем «печатающийся» пузырь в ошибку, если он пуст; иначе —
      // добавляем отдельный пузырь-ошибку, чтобы не терять частичный ответ.
      let target = bot;
      if (bot && bot.streaming && !bot.text) {
        bot.role = "error";
        bot.text = data.text;
        bot.streaming = false;
      } else {
        if (bot) bot.streaming = false;
        target = { role: "error", text: data.text, href: "", hrefLabel: "", href2: "", hrefLabel2: "" };
        this.messages.push(target);
      }
      if (data.href) {
        target.href = data.href;
        target.hrefLabel = data.hrefLabel || data.href;
      }
      if (data.href2) {
        target.href2 = data.href2;
        target.hrefLabel2 = data.hrefLabel2 || data.href2;
      }
      this.busy = false;
      this.$nextTick(() => this._scrollToEnd());
    },

    _finalize(bot) {
      if (bot) bot.streaming = false;
      if (bot && !bot.text && !bot.href) {
        // пустой ответ без ошибки — мягкая заглушка
        bot.text = t("err_empty_answer", "Пустой ответ.");
      }
      this.busy = false;
      this.$nextTick(() => this._scrollToEnd());
    },

    _closeStream() {
      if (this._es) {
        try { this._es.close(); } catch (e) { /* ignore */ }
        this._es = null;
      }
    },

    _scrollToEnd() {
      const log = this.$refs.log;
      if (log) log.scrollTop = log.scrollHeight;
    },
  };
}

// Экспортируем в window, чтобы Alpine (defer) нашёл фабрику по имени.
window.copilotWidget = copilotWidget;
