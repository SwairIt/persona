/* ============================================================
   Persona landing v2 — нативный скролл + GSAP ScrollTrigger.
   Без Lenis и без WebGL: снаппи, без джанка, «программистам зайдёт».
   Анимируем только transform/opacity. Уважает prefers-reduced-motion.
   ============================================================ */
(function () {
  'use strict';
  const root = document.body;
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  root.classList.add('js');
  requestAnimationFrame(() => root.classList.remove('preload'));

  // навигация: фон при скролле
  const nav = document.getElementById('nav');
  const onScroll = () => nav && nav.classList.toggle('scrolled', window.scrollY > 40);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  // ---- интерактивный демо-чат (работает всегда, даже при reduced-motion) ----
  (function () {
    const form = document.getElementById('demo-form');
    const input = document.getElementById('demo-input');
    const body = document.getElementById('demo-body');
    if (!form || !input || !body) return;
    const replies = [
      'Помню контекст: ты на этом и остановился вчера. В реальном аккаунте я подтяну твои файлы и историю и отвечу по делу.',
      'Это демо — но в Persona я держу твою память локально и в каждом чате знаю, над чем ты работаешь. Создай аккаунт, чтобы я помнил <b>именно тебя</b>.',
      'Хороший вопрос. С доступом к твоей памяти я отвечу с учётом твоих проектов, заметок и активности — а не общими словами.',
    ];
    let i = 0;
    const add = (cls, html) => {
      const d = document.createElement('div');
      d.className = 'msg ' + cls;
      d.innerHTML = html;
      body.appendChild(d);
      body.scrollTop = body.scrollHeight;
      return d;
    };
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      input.value = '';
      add('user', q.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c])));
      const typing = add('ai typing', '<span></span><span></span><span></span>');
      setTimeout(() => {
        typing.remove();
        add('ai', '<span class="ai-tag">Persona · помнит</span>' + replies[i % replies.length]);
        i++;
      }, 850);
    });
  })();

  // ---- 3D-наклон карточек на мышь + glare (cheap, только десктоп) ----
  if (window.matchMedia('(pointer:fine)').matches && !reduceMotion) {
    document.querySelectorAll('[data-tilt]').forEach((el) => {
      // блик, следующий за курсором
      const glare = document.createElement('span');
      glare.className = 'glare';
      el.appendChild(glare);
      el.addEventListener('pointermove', (e) => {
        const r = el.getBoundingClientRect();
        const px = (e.clientX - r.left) / r.width;
        const py = (e.clientY - r.top) / r.height;
        el.style.transform = `perspective(900px) rotateY(${(px - 0.5) * 8}deg) rotateX(${-(py - 0.5) * 8}deg) translateY(-5px)`;
        el.style.setProperty('--gx', (px * 100) + '%');
        el.style.setProperty('--gy', (py * 100) + '%');
      });
      el.addEventListener('pointerleave', () => { el.style.transform = ''; });
    });
  }

  // ---- параллакс плавающих 3D-объектов (скролл + курсор) ----
  (function () {
    const floaters = Array.from(document.querySelectorAll('.floater[data-float]'));
    if (!floaters.length || reduceMotion) return;
    let mx = 0, my = 0, sy = 0;
    const apply = () => {
      floaters.forEach((f) => {
        const k = parseFloat(f.dataset.float) || 0.1;
        const tx = mx * 40 * k;
        const ty = my * 40 * k - sy * k;
        const base = f.classList.contains('f-sq') ? ' rotate(18deg)' : '';
        f.style.transform = `translate3d(${tx}px,${ty}px,0)${base}`;
      });
    };
    if (window.matchMedia('(pointer:fine)').matches) {
      window.addEventListener('pointermove', (e) => {
        mx = e.clientX / window.innerWidth - 0.5;
        my = e.clientY / window.innerHeight - 0.5;
        apply();
      }, { passive: true });
    }
    window.addEventListener('scroll', () => { sy = window.scrollY * 0.15; apply(); }, { passive: true });
  })();

  const hasGSAP = typeof window.gsap !== 'undefined' && typeof window.ScrollTrigger !== 'undefined';

  // Нет анимаций / нет библиотек → показать всё сразу
  if (reduceMotion || !hasGSAP) {
    document.querySelectorAll('.reveal, [data-card], .big-statement .w')
      .forEach((el) => { el.style.opacity = '1'; el.style.transform = 'none'; });
    return;
  }

  const { gsap } = window;
  gsap.registerPlugin(window.ScrollTrigger);
  const ScrollTrigger = window.ScrollTrigger;

  // прогресс-бар
  const bar = document.getElementById('progress-bar');
  if (bar) {
    bar.style.transformOrigin = 'left';
    bar.style.width = '100%';
    ScrollTrigger.create({
      start: 0, end: 'max',
      onUpdate: (self) => { bar.style.transform = `scaleX(${self.progress})`; },
    });
  }

  // reveal-блоки
  gsap.utils.toArray('.reveal').forEach((el) => {
    gsap.to(el, {
      opacity: 1, y: 0, duration: 0.85, ease: 'power3.out',
      scrollTrigger: { trigger: el, start: 'top 88%', once: true },
    });
  });

  // карточки (bento) — со stagger
  gsap.to('[data-card]', {
    opacity: 1, y: 0, duration: 0.7, ease: 'power3.out', stagger: 0.1,
    scrollTrigger: { trigger: '.bento', start: 'top 82%', once: true },
  });

  // statement — слова всплывают по скроллу
  const words = gsap.utils.toArray('.big-statement .w');
  if (words.length) {
    gsap.to(words, {
      opacity: 1, y: 0, ease: 'none', stagger: 0.5,
      scrollTrigger: { trigger: '.statement', start: 'top 75%', end: 'bottom 60%', scrub: true },
    });
  }

  // count-up чисел в стат-бэнде
  gsap.utils.toArray('.stat .num').forEach((el) => {
    const m = el.textContent.trim().match(/^(\d+)(.*)$/);
    if (!m) return;
    const target = parseInt(m[1], 10), suffix = m[2] || '';
    const obj = { v: 0 };
    gsap.to(obj, {
      v: target, duration: 1.4, ease: 'power2.out',
      scrollTrigger: { trigger: el, start: 'top 90%', once: true },
      onUpdate: () => { el.textContent = Math.round(obj.v) + suffix; },
    });
  });

  // шаги — лёгкий parallax
  gsap.utils.toArray('.step').forEach((step, i) => {
    gsap.from(step, {
      y: 30 * (i + 1) / 2, ease: 'none',
      scrollTrigger: { trigger: '.steps', start: 'top bottom', end: 'top 45%', scrub: true },
    });
  });

  window.addEventListener('load', () => ScrollTrigger.refresh());
})();
