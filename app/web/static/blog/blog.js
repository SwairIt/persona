/* Persona blog — scrollspy + reading progress + smooth anchors.
   Никакого тяжёлого рендера: только IntersectionObserver и один scroll-listener
   на rAF. Уважает prefers-reduced-motion. */
(function () {
  'use strict';
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- прогресс чтения (rAF-throttled) ----
  const bar = document.querySelector('.read-progress');
  const article = document.querySelector('.post-body');
  if (bar && article) {
    let ticking = false;
    const update = () => {
      const rect = article.getBoundingClientRect();
      const total = rect.height - window.innerHeight;
      const passed = Math.min(Math.max(-rect.top, 0), Math.max(total, 1));
      bar.style.width = (total > 0 ? (passed / total) * 100 : 0) + '%';
      ticking = false;
    };
    window.addEventListener('scroll', () => {
      if (!ticking) { requestAnimationFrame(update); ticking = true; }
    }, { passive: true });
    update();
  }

  // ---- scrollspy оглавления ----
  const links = Array.from(document.querySelectorAll('.post-toc a[href^="#"]'));
  if (links.length) {
    const byId = new Map(links.map((a) => [a.getAttribute('href').slice(1), a]));
    const headings = links
      .map((a) => document.getElementById(a.getAttribute('href').slice(1)))
      .filter(Boolean);

    let current = null;
    const setActive = (id) => {
      if (id === current) return;
      current = id;
      links.forEach((a) => a.classList.toggle('active', a.getAttribute('href').slice(1) === id));
    };

    const io = new IntersectionObserver(
      (entries) => {
        // выбираем самый верхний видимый заголовок
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length) setActive(visible[0].target.id);
      },
      { rootMargin: '-80px 0px -70% 0px', threshold: 0 }
    );
    headings.forEach((h) => io.observe(h));

    // плавный скролл по клику
    links.forEach((a) => {
      a.addEventListener('click', (e) => {
        const el = document.getElementById(a.getAttribute('href').slice(1));
        if (!el) return;
        e.preventDefault();
        const y = el.getBoundingClientRect().top + window.scrollY - 84;
        window.scrollTo({ top: y, behavior: reduce ? 'auto' : 'smooth' });
        setActive(el.id);
        history.replaceState(null, '', '#' + el.id);
      });
    });
  }

  // ---- фильтр категорий на индексе ----
  const filter = document.querySelector('.cat-filter');
  if (filter) {
    const cards = Array.from(document.querySelectorAll('.post-card'));
    filter.addEventListener('click', (e) => {
      const btn = e.target.closest('.cat-btn');
      if (!btn) return;
      filter.querySelectorAll('.cat-btn').forEach((b) => b.classList.toggle('active', b === btn));
      const cat = btn.dataset.cat || '';
      cards.forEach((c) => {
        c.style.display = !cat || c.dataset.cat === cat ? '' : 'none';
      });
    });
  }
})();
