/* ============================================================
   Persona · Landing v2 — интеракции.
   IntersectionObserver-ревилы (GSAP опционален), демо-чат, навбар,
   tilt-карточки, count-up, скролл-скраб «statement». Чёрная дыра
   живёт в blackhole.js и сама слушает скролл/курсор.
   Анимируем только transform/opacity. Уважает prefers-reduced-motion.
   ============================================================ */
(function () {
  'use strict';
  var root = document.body;
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  root.classList.add('js');
  requestAnimationFrame(function () { root.classList.remove('preload'); });

  // навбар: фон при скролле
  var nav = document.getElementById('nav');
  function navScroll() { if (nav) nav.classList.toggle('scrolled', window.scrollY > 40); }
  addEventListener('scroll', navScroll, { passive: true });
  navScroll();

  // прогресс-бар (нативно, без GSAP)
  var bar = document.getElementById('progress-bar');
  if (bar) {
    addEventListener('scroll', function () {
      var h = document.documentElement, max = h.scrollHeight - h.clientHeight;
      bar.style.transform = 'scaleX(' + (max > 0 ? Math.min(window.scrollY / max, 1) : 0) + ')';
    }, { passive: true });
  }

  // ---- интерактивный демо-чат (работает всегда) ----
  (function () {
    var form = document.getElementById('demo-form');
    var input = document.getElementById('demo-input');
    var body = document.getElementById('demo-body');
    if (!form || !input || !body) return;
    var replies = [
      'Помню контекст: ты на этом и остановился вчера. В реальном аккаунте я подтяну твои файлы и историю и отвечу по делу.',
      'Это демо — но в Persona я держу твою память локально и в каждом чате знаю, над чем ты работаешь. Создай аккаунт, чтобы я помнил <b>именно тебя</b>.',
      'Хороший вопрос. С доступом к твоей памяти я отвечу с учётом твоих проектов, заметок и активности — а не общими словами.',
    ];
    var i = 0;
    function add(cls, html) {
      var d = document.createElement('div');
      d.className = 'msg ' + cls; d.innerHTML = html;
      body.appendChild(d); body.scrollTop = body.scrollHeight; return d;
    }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var q = input.value.trim(); if (!q) return; input.value = '';
      add('user', q.replace(/[<>&]/g, function (c) { return { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]; }));
      var typing = add('ai typing', '<span></span><span></span><span></span>');
      setTimeout(function () {
        typing.remove();
        add('ai', '<span class="ai-tag">Persona · помнит</span>' + replies[i % replies.length]); i++;
      }, 850);
    });
  })();

  // карточки тоже участвуют в reveal
  document.querySelectorAll('[data-card]').forEach(function (el) { el.classList.add('reveal'); });

  // reduced-motion → показать всё сразу
  if (reduce) {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
    document.querySelectorAll('.big-statement .w').forEach(function (w) { w.style.opacity = '1'; });
    return;
  }

  // ---- ревилы через IntersectionObserver (GSAP не обязателен) ----
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } });
    }, { threshold: 0.12, rootMargin: '0px 0px -7% 0px' });
    document.querySelectorAll('.reveal').forEach(function (el) { io.observe(el); });
  } else {
    document.querySelectorAll('.reveal').forEach(function (el) { el.classList.add('in'); });
  }

  // ---- tilt + glare (только десктоп, мягко) ----
  if (matchMedia('(pointer:fine)').matches) {
    document.querySelectorAll('[data-tilt]').forEach(function (el) {
      var glare = document.createElement('span'); glare.className = 'glare'; el.appendChild(glare);
      el.addEventListener('pointermove', function (e) {
        var r = el.getBoundingClientRect();
        var px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
        el.style.transform = 'perspective(900px) rotateY(' + ((px - 0.5) * 5) + 'deg) rotateX(' + (-(py - 0.5) * 5) + 'deg) translateY(-4px)';
        el.style.setProperty('--gx', (px * 100) + '%');
        el.style.setProperty('--gy', (py * 100) + '%');
      });
      el.addEventListener('pointerleave', function () { el.style.transform = ''; });
    });
  }

  // ---- statement + count-up: GSAP если есть, иначе нативно ----
  var hasGSAP = typeof window.gsap !== 'undefined' && typeof window.ScrollTrigger !== 'undefined';
  if (hasGSAP) {
    var gsap = window.gsap; gsap.registerPlugin(window.ScrollTrigger);
    var words = gsap.utils.toArray('.big-statement .w');
    if (words.length) {
      gsap.to(words, {
        opacity: 1, ease: 'none', stagger: 0.5,
        scrollTrigger: { trigger: '.statement', start: 'top 78%', end: 'bottom 62%', scrub: true },
      });
    }
    gsap.utils.toArray('.stat .num').forEach(function (el) {
      var m = el.textContent.trim().match(/^(\d+)(.*)$/); if (!m) return;
      var t = parseInt(m[1], 10), s = m[2] || '', o = { v: 0 };
      gsap.to(o, {
        v: t, duration: 1.4, ease: 'power2.out',
        scrollTrigger: { trigger: el, start: 'top 92%', once: true },
        onUpdate: function () { el.textContent = Math.round(o.v) + s; },
      });
    });
    addEventListener('load', function () { window.ScrollTrigger.refresh(); });
  } else {
    // без GSAP: слова statement проявляются разом при входе
    var stEl = document.querySelector('.statement');
    if (stEl && 'IntersectionObserver' in window) {
      var sio = new IntersectionObserver(function (es) {
        es.forEach(function (e) {
          if (e.isIntersecting) {
            document.querySelectorAll('.big-statement .w').forEach(function (w, k) {
              setTimeout(function () { w.style.opacity = '1'; }, k * 70);
            });
            sio.disconnect();
          }
        });
      }, { threshold: 0.4 });
      sio.observe(stEl);
    } else if (stEl) {
      document.querySelectorAll('.big-statement .w').forEach(function (w) { w.style.opacity = '1'; });
    }
  }
})();
