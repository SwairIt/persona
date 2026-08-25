/* Persona — согласие на аналитические cookie (152-ФЗ / ст. 9).
 *
 * Зачем это существует
 * --------------------
 * На публичных страницах стоит счётчик Яндекс.Метрики 111901324 с ВЕБВИЗОРОМ:
 * он пишет сессию — движения мыши, клики, ввод, прокрутку. Это обработка
 * персональных данных, и по 152-ФЗ она требует ИНФОРМИРОВАННОГО согласия
 * ДО начала обработки, а не после. Поэтому счётчик не должен загружаться,
 * пока человек не нажал «Принять».
 *
 * Как устроена защита (два рубежа, оба обязательны)
 * ------------------------------------------------
 * 1. СЕРВЕР. ``_metrika.html`` вставляет код счётчика в HTML только если в
 *    запросе пришла кука ``persona_consent=all``. Без согласия в HTML нет ни
 *    ``tag.js``, ни noscript-пикселя — то есть браузер физически не ходит к
 *    Яндексу, даже если JS отключён.
 * 2. КЛИЕНТ (этот файл). Рисует баннер, запоминает решение в куку +
 *    localStorage и, если человек нажал «Принять», подгружает счётчик СРАЗУ,
 *    без перезагрузки страницы.
 *
 * Почему кука, а не только localStorage: решение должно быть видно СЕРВЕРУ на
 * следующем рендере — иначе он не сможет держать рубеж №1. Сама эта кука
 * строго необходимая (хранит отказ/согласие, ничего не отслеживает), поэтому
 * её постановка согласия не требует.
 *
 * Отказ ровно так же лёгок, как согласие: две равнозначные кнопки, никаких
 * предвыбранных галочек, никакого «продолжая пользоваться — вы согласны».
 */
(function () {
  'use strict';

  var COOKIE = 'persona_consent';
  var LS_KEY = 'persona_consent';
  var MAX_AGE = 60 * 60 * 24 * 180; // 180 дней — потом спросим заново
  var YM_HOST = 'https://mc.yandex.ru';

  // id счётчика приходит из data-атрибута тега <script> (см. _metrika.html),
  // чтобы номер жил в одном месте — в шаблоне.
  var self = document.currentScript;
  var YM_ID = (self && self.getAttribute('data-ym-id')) || '';

  /* ── хранилище решения ───────────────────────────────────────────────── */

  function readCookie(name) {
    var m = document.cookie.match('(?:^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[1]) : '';
  }

  function readLS() {
    try { return window.localStorage.getItem(LS_KEY) || ''; } catch (e) { return ''; }
  }

  // Кука — источник истины (её видит сервер). localStorage — зеркало на
  // случай, если куку сняли сторонние чистилки, но выбор человек уже сделал.
  function decision() {
    var v = readCookie(COOKIE) || readLS();
    return (v === 'all' || v === 'necessary') ? v : '';
  }

  function remember(value) {
    document.cookie = COOKIE + '=' + value + '; path=/; max-age=' + MAX_AGE +
      '; SameSite=Lax' + (location.protocol === 'https:' ? '; Secure' : '');
    try { window.localStorage.setItem(LS_KEY, value); } catch (e) { /* приватный режим */ }
  }

  function forget() {
    document.cookie = COOKIE + '=; path=/; max-age=0; SameSite=Lax';
    try { window.localStorage.removeItem(LS_KEY); } catch (e) { /* ignore */ }
  }

  /* ── загрузка счётчика (только после согласия) ───────────────────────── */

  function counterAlreadyLoaded() {
    if (window.ym && window.ym.a) return true;
    for (var i = 0; i < document.scripts.length; i++) {
      if ((document.scripts[i].src || '').indexOf('/metrika/tag.js') > -1) return true;
    }
    return false;
  }

  function loadCounter() {
    if (!YM_ID || counterAlreadyLoaded()) return;
    var src = YM_HOST + '/metrika/tag.js?id=' + YM_ID;
    window.ym = window.ym || function () { (window.ym.a = window.ym.a || []).push(arguments); };
    window.ym.l = 1 * new Date();
    var s = document.createElement('script');
    s.async = 1;
    s.src = src;
    (document.head || document.documentElement).appendChild(s);
    window.ym(Number(YM_ID), 'init', {
      ssr: true, webvisor: true, clickmap: true, ecommerce: 'dataLayer',
      referrer: document.referrer, url: location.href,
      accurateTrackBounce: true, trackLinks: true
    });
  }

  /* ── баннер ──────────────────────────────────────────────────────────── */

  var banner = null;

  var CSS = [
    '#persona-consent{position:fixed;left:0;right:0;bottom:0;z-index:2147483000;',
    'background:#0d0d16;color:#e8e9f5;border-top:1px solid rgba(255,255,255,.14);',
    'box-shadow:0 -8px 40px rgba(0,0,0,.5);font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}',
    '#persona-consent .pc-in{max-width:1000px;margin:0 auto;padding:16px 20px;display:flex;',
    'gap:18px;align-items:center;flex-wrap:wrap}',
    '#persona-consent .pc-txt{flex:1 1 380px;min-width:260px;margin:0;color:#c9cbe0}',
    '#persona-consent .pc-txt b{color:#fff;font-weight:600}',
    '#persona-consent a{color:#a78bfa;text-decoration:underline}',
    '#persona-consent .pc-btns{display:flex;gap:10px;flex-wrap:wrap}',
    '#persona-consent button{font:inherit;font-weight:600;padding:10px 18px;border-radius:9px;',
    'border:1px solid rgba(255,255,255,.22);background:transparent;color:#e8e9f5;cursor:pointer}',
    '#persona-consent button:hover{border-color:#a78bfa}',
    '#persona-consent button.pc-yes{background:#7c3aed;border-color:#7c3aed;color:#fff}',
    '#persona-consent button.pc-yes:hover{background:#8b5cf6}',
    '@media(max-width:560px){#persona-consent .pc-btns{width:100%}',
    '#persona-consent .pc-btns button{flex:1 1 0}}'
  ].join('');

  function injectCSS() {
    if (document.getElementById('persona-consent-css')) return;
    var st = document.createElement('style');
    st.id = 'persona-consent-css';
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  function render() {
    if (banner) { banner.hidden = false; return; }
    injectCSS();
    banner = document.createElement('aside');
    banner.id = 'persona-consent';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-live', 'polite');
    banner.setAttribute('aria-label', 'Согласие на аналитические cookie');
    banner.innerHTML =
      '<div class="pc-in">' +
        '<p class="pc-txt">Мы хотим включить <b>Яндекс.Метрику</b> — она ставит cookie и через ' +
        '<b>вебвизор</b> записывает, как вы пользуетесь страницей: движения мыши, клики, прокрутку, ' +
        'ввод в поля. Это персональные данные, поэтому спрашиваем заранее. ' +
        'Без согласия счётчик не загрузится вообще. ' +
        'Технические cookie (вход в аккаунт, защита форм) работают всегда — без них сайт не работает. ' +
        '<a href="/privacy-policy/cookies">Подробнее о cookie</a> · ' +
        '<a href="/privacy-policy">Политика конфиденциальности</a></p>' +
        '<div class="pc-btns">' +
          '<button type="button" class="pc-no">Только необходимые</button>' +
          '<button type="button" class="pc-yes">Принять</button>' +
        '</div>' +
      '</div>';
    banner.querySelector('.pc-yes').addEventListener('click', function () { choose('all'); });
    banner.querySelector('.pc-no').addEventListener('click', function () { choose('necessary'); });
    (document.body || document.documentElement).appendChild(banner);
  }

  function hide() { if (banner) banner.hidden = true; }

  function choose(value) {
    remember(value);
    hide();
    if (value === 'all') loadCounter();
    try {
      document.dispatchEvent(new CustomEvent('persona:consent', { detail: { value: value } }));
    } catch (e) { /* старые браузеры — событие необязательное */ }
  }

  /* ── публичный API (ссылка «изменить решение» в подвале / на /cookies) ─ */

  window.PersonaConsent = {
    get: decision,
    accept: function () { choose('all'); },
    reject: function () { choose('necessary'); },
    // Отозвать согласие и спросить заново. Счётчик, уже загруженный на этой
    // странице, продолжит работать до перезагрузки — поэтому перезагружаем.
    reset: function (reload) {
      forget();
      if (reload !== false) { location.reload(); return; }
      render();
    },
    open: render
  };

  function wireOpeners() {
    var nodes = document.querySelectorAll('[data-consent-open]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].addEventListener('click', function (ev) {
        ev.preventDefault();
        window.PersonaConsent.reset(false);
      });
    }
    var status = document.querySelectorAll('[data-consent-status]');
    for (var j = 0; j < status.length; j++) {
      var d = decision();
      status[j].textContent = d === 'all'
        ? 'Аналитика разрешена (Яндекс.Метрика с вебвизором включена).'
        : (d === 'necessary'
            ? 'Аналитика отключена — работают только необходимые cookie.'
            : 'Решение ещё не принято — аналитика пока отключена.');
    }
  }

  function boot() {
    wireOpeners();
    // Без id счётчика скрипт работает в «пассивном» режиме: только ссылка
    // «изменить решение» и текст статуса. Так его можно подключать на
    // страницах, где Метрики нет вовсе (кабинет участника) — баннер там
    // спрашивал бы согласие на то, чего не происходит.
    if (!YM_ID) return;
    var d = decision();
    if (d === 'all') { loadCounter(); return; }  // сервер мог не увидеть куку
    if (d === 'necessary') return;               // отказ — молчим
    render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
