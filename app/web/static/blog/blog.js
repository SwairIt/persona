/* Persona blog — прогресс чтения, оглавление со скроллспаем, якоря заголовков,
   мобильный ящик оглавления и живой фильтр на листинге.

   Никакого тяжёлого рендера: один scroll-listener на rAF + IntersectionObserver.
   Уважает prefers-reduced-motion. Всё, что делает этот файл, — УЛУЧШЕНИЕ:
   без JS оглавление остаётся обычным списком ссылок, мобильный ящик
   открывается настоящим checkbox'ом, а поиск на листинге — обычной формой,
   которую отрисовывает сервер. */
(function () {
  'use strict';
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const NAV_OFFSET = 92; /* высота плавающей пилюли-нава + воздух */

  const article = document.querySelector('.post-body');

  // ------------------------------------------------------------------
  // 1. Якоря заголовков (кликабельная решётка).
  //    Шаблон уже вставляет их серверно по post.toc; здесь — страховка на
  //    случай, если движок сменил разметку заголовка. Идемпотентно.
  // ------------------------------------------------------------------
  if (article) {
    article.querySelectorAll('h2[id], h3[id]').forEach(function (h) {
      if (h.querySelector('.anchor-link')) return;
      const a = document.createElement('a');
      a.className = 'anchor-link';
      a.href = '#' + h.id;
      a.setAttribute('aria-label', 'Ссылка на раздел «' + h.textContent.trim() + '»');
      a.textContent = '#';
      h.insertBefore(a, h.firstChild);
    });

    // широкая таблица должна скроллиться сама, а не тащить страницу
    article.querySelectorAll('table').forEach(function (t) {
      if (t.parentElement && t.parentElement.classList.contains('table-scroll')) return;
      const box = document.createElement('div');
      box.className = 'table-scroll';
      t.parentNode.insertBefore(box, t);
      box.appendChild(t);
    });
  }

  // ------------------------------------------------------------------
  // 2. Оглавление: скроллспай + прогресс внутри списка
  // ------------------------------------------------------------------
  const toc = document.querySelector('.post-toc');
  const links = Array.from(document.querySelectorAll('.post-toc a[href^="#"]'));
  const headings = links
    .map(function (a) { return document.getElementById(a.getAttribute('href').slice(1)); })
    .filter(Boolean);
  const scroller = document.querySelector('.toc-scroll');
  const pctEls = Array.from(document.querySelectorAll('.toc-pct'));

  let current = null;
  function setActive(id) {
    if (id === current) return;
    current = id;
    let active = null;
    links.forEach(function (a) {
      const on = a.getAttribute('href').slice(1) === id;
      a.classList.toggle('is-active', on);
      if (on) { a.setAttribute('aria-current', 'true'); active = a; }
      else { a.removeAttribute('aria-current'); }
    });
    // держим активный пункт в поле зрения собственной прокрутки оглавления
    if (active && scroller && scroller.scrollHeight > scroller.clientHeight) {
      const box = scroller.getBoundingClientRect();
      const item = active.getBoundingClientRect();
      if (item.top < box.top + 8 || item.bottom > box.bottom - 8) {
        scroller.scrollTo({
          top: scroller.scrollTop + (item.top - box.top) - box.height / 2 + item.height / 2,
          behavior: reduce ? 'auto' : 'smooth',
        });
      }
    }
  }

  // ------------------------------------------------------------------
  // 3. Прогресс чтения (rAF-throttled): полоса сверху + заливка рельсы
  //    оглавления + процент. Один обработчик на всё.
  // ------------------------------------------------------------------
  const bar = document.querySelector('.read-progress');
  if (article && (bar || toc || pctEls.length)) {
    let ticking = false;
    let lastPct = -1;

    const update = function () {
      ticking = false;
      const rect = article.getBoundingClientRect();
      const total = rect.height - window.innerHeight;
      const passed = Math.min(Math.max(-rect.top, 0), Math.max(total, 1));
      const ratio = total > 0 ? passed / total : (rect.top <= 0 ? 1 : 0);
      const pct = Math.round(Math.min(Math.max(ratio, 0), 1) * 100);

      if (bar) bar.style.width = pct + '%';
      if (pct !== lastPct) {
        lastPct = pct;
        if (toc) toc.style.setProperty('--toc-progress', pct + '%');
        pctEls.forEach(function (el) { el.textContent = pct + '%'; });
        if (bar) bar.setAttribute('aria-valuenow', String(pct));
      }

      // прочитанные пункты оглавления гасим иначе, чем непрочитанные
      for (let i = 0; i < headings.length; i += 1) {
        links[i].classList.toggle(
          'is-read',
          headings[i].getBoundingClientRect().top < NAV_OFFSET + 8
        );
      }
    };

    window.addEventListener('scroll', function () {
      if (!ticking) { requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    window.addEventListener('resize', function () {
      if (!ticking) { requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
  }

  if (links.length && headings.length) {
    const io = new IntersectionObserver(
      function (entries) {
        // выбираем самый верхний видимый заголовок
        const visible = entries
          .filter(function (e) { return e.isIntersecting; })
          .sort(function (a, b) { return a.boundingClientRect.top - b.boundingClientRect.top; });
        if (visible.length) setActive(visible[0].target.id);
      },
      { rootMargin: '-' + NAV_OFFSET + 'px 0px -68% 0px', threshold: 0 }
    );
    headings.forEach(function (h) { io.observe(h); });
  }

  // ------------------------------------------------------------------
  // 4. Мобильный ящик оглавления.
  //    Открытие/закрытие делает сам checkbox (работает без JS) — здесь мы
  //    добавляем то, чего CSS не умеет: закрытие по выбору пункта, Escape,
  //    блокировку прокрутки фона и корректный aria-expanded.
  // ------------------------------------------------------------------
  const tocCheck = document.getElementById('toc-toggle');
  const tocFab = document.querySelector('.toc-fab');

  function syncDrawer() {
    if (!tocCheck) return;
    const open = tocCheck.checked;
    document.body.classList.toggle('toc-open', open);
    if (tocFab) tocFab.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (toc) toc.setAttribute('aria-hidden', open || window.innerWidth > 1080 ? 'false' : 'true');
  }
  function closeDrawer() {
    if (tocCheck && tocCheck.checked) { tocCheck.checked = false; syncDrawer(); }
  }
  if (tocCheck) {
    tocCheck.addEventListener('change', syncDrawer);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeDrawer();
    });
    // поворот телефона / переход через брейкпоинт: ящик закрываем, чтобы
    // на широком экране не остался «открытым» скрытый оверлей
    window.addEventListener('resize', function () {
      if (window.innerWidth > 1080) closeDrawer();
      else syncDrawer();
    }, { passive: true });
    syncDrawer();
  }

  // ------------------------------------------------------------------
  // 5. Плавный переход по клику в оглавлении (+ закрытие ящика)
  // ------------------------------------------------------------------
  links.forEach(function (a) {
    a.addEventListener('click', function (e) {
      const el = document.getElementById(a.getAttribute('href').slice(1));
      if (!el) return;
      e.preventDefault();
      closeDrawer();
      const y = el.getBoundingClientRect().top + window.scrollY - NAV_OFFSET;
      window.scrollTo({ top: y, behavior: reduce ? 'auto' : 'smooth' });
      setActive(el.id);
      history.replaceState(null, '', '#' + el.id);
    });
  });

  // ------------------------------------------------------------------
  // 6. Живой фильтр на листинге — НАДСТРОЙКА над серверным поиском.
  //    Форма остаётся формой: Enter уводит на /blog/search?q=…, который
  //    рисует сервер и который работает с выключенным JS.
  // ------------------------------------------------------------------
  const liveInput = document.querySelector('input[data-live-filter]');
  if (liveInput) {
    const cards = Array.from(document.querySelectorAll('.post-grid [data-search]'));
    const counter = document.querySelector('[data-live-count]');
    const emptyBox = document.querySelector('[data-live-empty]');
    const countTpl = counter ? counter.getAttribute('data-live-count') : '';

    const apply = function () {
      const q = liveInput.value.trim().toLowerCase();
      let shown = 0;
      cards.forEach(function (c) {
        const hit = !q || (c.getAttribute('data-search') || '').indexOf(q) !== -1;
        c.hidden = !hit;
        if (hit) shown += 1;
      });
      if (counter && countTpl) counter.textContent = countTpl.replace('%s', String(shown));
      if (emptyBox) emptyBox.hidden = shown !== 0;
    };
    liveInput.addEventListener('input', apply);
    if (liveInput.value) apply();
  }
})();
