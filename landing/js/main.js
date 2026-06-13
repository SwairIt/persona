/* ============================================================
   Persona — оркестрация скролла (Lenis + GSAP ScrollTrigger)
   Скиллы scroll-experience / gsap-framer:
   - scrub только с ease:none; анимируем transform/opacity
   - pinned-герой ведёт прогресс 3D-сцены
   - parallax-слои разной скорости; reveal по входу
   - prefers-reduced-motion → контент сразу виден, движения нет
   ============================================================ */
(function () {
  'use strict';

  const root = document.body;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // год в футере
  const yearEl = document.querySelector('.footer');
  if (yearEl) yearEl.innerHTML = yearEl.innerHTML.replace('{год}', new Date().getFullYear());

  // помечаем, что JS есть (включает стартовое скрытие .reveal), снимаем прелоад
  root.classList.add('js');
  requestAnimationFrame(() => root.classList.remove('preload'));

  // --- навигация: фон при скролле (работает даже без GSAP) ---
  const nav = document.getElementById('nav');
  const onScrollNav = () => {
    if (!nav) return;
    nav.classList.toggle('scrolled', window.scrollY > 40);
  };
  window.addEventListener('scroll', onScrollNav, { passive: true });
  onScrollNav();

  // ============================================================
  // REDUCED MOTION / нет библиотек → просто показать всё, без анимаций
  // ============================================================
  const hasGSAP = typeof window.gsap !== 'undefined' && typeof window.ScrollTrigger !== 'undefined';

  if (reduceMotion || !hasGSAP) {
    document.querySelectorAll('.reveal, [data-card], .big-statement .w')
      .forEach((el) => { el.style.opacity = '1'; el.style.transform = 'none'; });
    return;
  }

  const { gsap } = window;
  gsap.registerPlugin(window.ScrollTrigger);
  const ScrollTrigger = window.ScrollTrigger;

  // ============================================================
  // Lenis — плавный скролл, синхронизированный с ScrollTrigger
  // ============================================================
  let lenis = null;
  if (typeof window.Lenis !== 'undefined') {
    lenis = new window.Lenis({
      duration: 1.1,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      smoothWheel: true,
      // на тач-устройствах оставляем нативный скролл (стабильнее, см. scroll-experience)
      smoothTouch: false,
    });
    lenis.on('scroll', ScrollTrigger.update);
    gsap.ticker.add((time) => lenis.raf(time * 1000));
    gsap.ticker.lagSmoothing(0);

    // якорные ссылки через Lenis
    document.querySelectorAll('a[href^="#"]').forEach((a) => {
      a.addEventListener('click', (e) => {
        const id = a.getAttribute('href');
        if (id.length < 2) return;
        const target = document.querySelector(id);
        if (!target) return;
        e.preventDefault();
        lenis.scrollTo(target, { offset: -10 });
      });
    });
  }

  // ============================================================
  // Прогресс-бар скролла
  // ============================================================
  const bar = document.getElementById('progress-bar');
  if (bar) {
    ScrollTrigger.create({
      start: 0, end: 'max',
      onUpdate: (self) => { bar.style.transform = `scaleX(${self.progress})`; bar.style.width = '100%'; bar.style.transformOrigin = 'left'; },
    });
  }

  // ============================================================
  // Reveal-блоки по входу во вьюпорт (.reveal)
  // ============================================================
  gsap.utils.toArray('.reveal').forEach((el) => {
    gsap.to(el, {
      opacity: 1, y: 0, duration: 0.9, ease: 'power3.out',
      scrollTrigger: { trigger: el, start: 'top 85%', once: true },
    });
  });

  // карточки фич — со stagger по контейнеру
  gsap.to('[data-card]', {
    opacity: 1, y: 0, duration: 0.8, ease: 'power3.out', stagger: 0.12,
    scrollTrigger: { trigger: '.cards', start: 'top 80%', once: true },
  });

  // ============================================================
  // HERO — pinned, scrub; прогресс ведёт 3D-сцену
  // ============================================================
  const hero = document.getElementById('hero');
  if (hero) {
    // контент героя уезжает/тает по мере скролла (только transform/opacity)
    gsap.timeline({
      scrollTrigger: {
        trigger: hero,
        start: 'top top',
        end: '+=120%',
        pin: true,
        scrub: 1,
        anticipatePin: 1,
        onUpdate: (self) => {
          if (window.PersonaScene && window.PersonaScene.ready) {
            window.PersonaScene.setHeroProgress(self.progress);
          }
        },
      },
    })
    .to('.hero-inner', { y: -60, opacity: 0, scale: 0.94, ease: 'none' })
    .to('.scroll-hint', { opacity: 0, ease: 'none' }, 0);
  }

  // ============================================================
  // STATEMENT — слова всплывают по очереди, привязаны к скроллу
  // ============================================================
  const words = gsap.utils.toArray('.big-statement .w');
  if (words.length) {
    gsap.to(words, {
      opacity: 1, y: 0, ease: 'none', stagger: 0.5,
      scrollTrigger: {
        trigger: '.statement',
        start: 'top 70%',
        end: 'bottom 60%',
        scrub: true,
      },
    });
  }

  // ============================================================
  // Parallax фоновых blob'ов (слои разной скорости, scroll-experience)
  // ============================================================
  const layers = [
    { el: '.blob-1', y: -120 },
    { el: '.blob-2', y: 180 },
    { el: '.blob-3', y: -90 },
  ];
  layers.forEach((L) => {
    const node = document.querySelector(L.el);
    if (!node) return;
    gsap.to(node, {
      yPercent: L.y / 6, ease: 'none',
      scrollTrigger: { start: 0, end: 'max', scrub: true },
    });
  });

  // hue-сдвиг фона по секциям — «каждый скролл меняет фон»
  const sections = gsap.utils.toArray('section[data-section]');
  const hues = [255, 280, 200, 230, 300, 260];
  sections.forEach((sec, i) => {
    ScrollTrigger.create({
      trigger: sec,
      start: 'top center',
      end: 'bottom center',
      onToggle: (self) => {
        if (self.isActive) {
          gsap.to('.bg-mesh', { filter: `hue-rotate(${hues[i] - 255}deg)`, duration: 1.2, ease: 'power2.out' });
        }
      },
    });
  });

  // карточки шага — лёгкий parallax
  gsap.utils.toArray('.step').forEach((step, i) => {
    gsap.from(step, {
      y: 40 * (i + 1) / 2, ease: 'none',
      scrollTrigger: { trigger: '.steps', start: 'top bottom', end: 'top 40%', scrub: true },
    });
  });

  // пересчёт после полной загрузки (картинки/шрифты могли сдвинуть высоты)
  window.addEventListener('load', () => ScrollTrigger.refresh());
})();
