/* Persona — первосторонний счётчик действий (клики, сабмиты, исходящие ссылки).
 *
 * Это НЕ запись сессии. Здесь нет и не будет обработчиков mousemove, scroll,
 * keydown и чтения значений полей: собирается только «на что нажали», где
 * «что» — это подпись, которую разработчик сам написал в data-track. Полный
 * разбор решения — в докстринге app/analytics/capture.py.
 *
 * Что попадает в отправку
 * -----------------------
 *   1. клик по элементу с атрибутом [data-track] → label = значение атрибута;
 *   2. submit любой формы            → label = data-track | id | action формы;
 *   3. клик по ссылке на ЧУЖОЙ хост  → label = хост (без пути и query).
 *
 * Чего НЕ попадает
 * ----------------
 *   * значения полей формы — ни одного, никогда;
 *   * текст ссылки/кнопки, если на ней нет data-track (иначе в счётчик
 *     утекали бы имена людей и заголовки чужих чатов из подписей элементов);
 *   * полный URL исходящей ссылки — только хост.
 *
 * Почему пачкой
 * -------------
 * События копятся и уходят одним POST раз в FLUSH_MS (или при уходе со
 * страницы). Активная страница не имеет права превращаться в поток запросов:
 * на этом сервере POST участника ещё и считает троттл
 * (app/web/middleware/throttle.py).
 *
 * Почему fetch(keepalive), а не sendBeacon
 * ----------------------------------------
 * sendBeacon не умеет ставить заголовки, а CSRF-middleware ждёт
 * X-CSRF-Token на любом небезопасном методе с сессией. csrf.js патчит
 * window.fetch и проставляет заголовок сам — поэтому этот файл обязан
 * грузиться ПОСЛЕ csrf.js. keepalive даёт то же «доживёт до выгрузки», что и
 * sendBeacon.
 */
(function () {
  'use strict';

  var ENDPOINT = '/api/track';
  var FLUSH_MS = 4000;
  var MAX_BATCH = 20;
  var MAX_LABEL = 120;

  var queue = [];
  var timer = null;

  function trim(value) {
    return String(value == null ? '' : value).slice(0, MAX_LABEL);
  }

  function push(kind, label) {
    if (!label) return;
    if (queue.length >= MAX_BATCH) return; // переполнение — молча роняем
    queue.push({ kind: kind, label: trim(label), path: location.pathname });
    if (timer === null) timer = window.setTimeout(flush, FLUSH_MS);
  }

  function flush(final) {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    if (!queue.length) return;
    var body = JSON.stringify({ events: queue.splice(0, MAX_BATCH) });
    try {
      window.fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
        keepalive: true,
        credentials: 'same-origin'
      })['catch'](function () { /* счётчик молчит про свои неудачи */ });
    } catch (e) { /* и про эти тоже */ }
  }

  /* ── 1. клик по [data-track] ─────────────────────────────────────────── */
  document.addEventListener(
    'click',
    function (event) {
      var node = event.target;
      var tracked = null;
      // Поднимаемся максимум на 5 уровней: клик почти всегда приходит в
      // <span> внутри кнопки, а обход всего дерева до <html> на каждый клик
      // — лишняя работа на ровном месте.
      for (var depth = 0; node && node !== document && depth < 5; depth++) {
        if (node.getAttribute && node.getAttribute('data-track')) {
          tracked = node;
          break;
        }
        node = node.parentNode;
      }
      if (tracked) {
        push('click', tracked.getAttribute('data-track'));
        return;
      }
      // 3. исходящая ссылка — считаем только ХОСТ.
      var link = event.target.closest ? event.target.closest('a[href]') : null;
      if (!link) return;
      try {
        var url = new URL(link.getAttribute('href'), location.href);
        if (url.origin && url.origin !== location.origin) {
          push('outbound', url.host);
        }
      } catch (e) { /* битый href — не наше дело */ }
    },
    true
  );

  /* ── 2. отправка формы ───────────────────────────────────────────────── */
  document.addEventListener(
    'submit',
    function (event) {
      var form = event.target;
      if (!form || !form.getAttribute) return;
      var label =
        form.getAttribute('data-track') ||
        form.getAttribute('id') ||
        form.getAttribute('action') ||
        'form';
      push('submit', label);
      // Форма уходит прямо сейчас — не ждём таймер, иначе событие уедет
      // вместе со страницей.
      flush(true);
    },
    true
  );

  /* ── уход со страницы ────────────────────────────────────────────────── */
  window.addEventListener('pagehide', function () { flush(true); });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') flush(true);
  });
})();
