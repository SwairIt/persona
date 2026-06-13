/* Obsidian-стиль граф памяти для лендинга (canvas-2D, без зависимостей).
   Force-directed: отталкивание узлов + пружины связей + центрирование.
   Дёшево и плавно (≈40 узлов), интерактивно: таскай узлы, наводи —
   подсвечиваются связи. Ленивая инициализация + пауза вне экрана. */

const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const canvas = document.getElementById('graph-canvas');

if (canvas) {
  let started = false;
  const io = new IntersectionObserver((es) => {
    if (es[0].isIntersecting && !started) { started = true; io.disconnect(); start(); }
  }, { threshold: 0.15 });
  io.observe(canvas);
}

function start() {
  const ctx = canvas.getContext('2d');
  if (!ctx) { canvas.style.display = 'none'; return; }

  const TYPES = {
    prompt: { c: '167,139,250', label: 'промпт' },
    answer: { c: '94,234,212', label: 'ответ' },
    memory: { c: '244,114,182', label: 'память' },
    day:    { c: '251,191,36', label: 'день записи' },
  };

  // --- генерация демо-графа (структура как у настоящего) ---
  const nodes = [];
  const links = [];
  const add = (type, label, r) => { const n = { type, label, r: r || 5, x: 0, y: 0, vx: 0, vy: 0 }; nodes.push(n); return nodes.length - 1; };
  const link = (a, b) => links.push({ a, b });
  const rnd = (() => { let s = 7; return () => (s = (s * 16807) % 2147483647) / 2147483647; })(); // детерминированный

  const dayLabels = ['Пн', 'Вт', 'Ср', 'Чт'];
  const promptText = ['лендинг', 'блог', 'архитектура', 'память', 'модель', 'приватность', 'файлы', 'дизайн', 'фикс бага', 'идея'];
  const days = dayLabels.map((d) => add('day', d, 9));
  let memHub = add('memory', 'карточка дня', 8);
  days.forEach((d) => link(d, memHub));

  const prompts = [];
  days.forEach((d) => {
    const k = 3 + Math.floor(rnd() * 3);
    for (let i = 0; i < k; i++) {
      const p = add('prompt', promptText[Math.floor(rnd() * promptText.length)], 5.5);
      link(d, p);
      const a = add('answer', 'ответ', 4.5);
      link(p, a);
      prompts.push(p);
      if (rnd() > 0.6) link(a, memHub); // часть ответов попадает в память
    }
  });
  // кросс-связи между похожими промптами (как смысловые)
  for (let i = 0; i < prompts.length; i++) {
    if (rnd() > 0.7) {
      const j = Math.floor(rnd() * prompts.length);
      if (j !== i) link(prompts[i], prompts[j]);
    }
  }

  // соседи (для подсветки)
  const neigh = nodes.map(() => new Set());
  links.forEach((l) => { neigh[l.a].add(l.b); neigh[l.b].add(l.a); });

  // --- размеры / DPR ---
  let W = 0, H = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
  function resize() {
    const stage = canvas.parentElement;
    W = stage.clientWidth; H = stage.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  window.addEventListener('resize', () => { resize(); }, { passive: true });

  // начальная раскладка по кругу
  nodes.forEach((n, i) => {
    const a = (i / nodes.length) * Math.PI * 2;
    n.x = W / 2 + Math.cos(a) * Math.min(W, H) * 0.3 + (Math.random ? 0 : 0);
    n.y = H / 2 + Math.sin(a) * Math.min(W, H) * 0.3;
  });
  // детерминированный джиттер
  nodes.forEach((n) => { n.x += (rnd() - 0.5) * 40; n.y += (rnd() - 0.5) * 40; });

  // --- взаимодействие ---
  let hover = -1, drag = -1;
  const pos = (e) => { const r = canvas.getBoundingClientRect(); const t = e.touches ? e.touches[0] : e; return { x: t.clientX - r.left, y: t.clientY - r.top }; };
  const pick = (p) => {
    let best = -1, bd = 18 * 18;
    for (let i = 0; i < nodes.length; i++) { const dx = nodes[i].x - p.x, dy = nodes[i].y - p.y, d = dx * dx + dy * dy; if (d < bd) { bd = d; best = i; } }
    return best;
  };
  canvas.style.touchAction = 'none';
  canvas.addEventListener('pointerdown', (e) => { const p = pos(e); drag = pick(p); if (drag >= 0) canvas.classList.add('grabbing'); });
  canvas.addEventListener('pointermove', (e) => { const p = pos(e); if (drag >= 0) { nodes[drag].x = p.x; nodes[drag].y = p.y; nodes[drag].vx = nodes[drag].vy = 0; } else { hover = pick(p); canvas.style.cursor = hover >= 0 ? 'grab' : 'default'; } });
  window.addEventListener('pointerup', () => { drag = -1; canvas.classList.remove('grabbing'); });
  canvas.addEventListener('pointerleave', () => { if (drag < 0) hover = -1; });

  // --- физика ---
  function step() {
    const cx = W / 2, cy = H / 2;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y; let d2 = dx * dx + dy * dy || 0.01;
        const f = 900 / d2; const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    links.forEach((l) => {
      const a = nodes[l.a], b = nodes[l.b];
      let dx = b.x - a.x, dy = b.y - a.y; const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - 70) * 0.02; const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    });
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      if (i === drag) continue;
      n.vx += (cx - n.x) * 0.0015; n.vy += (cy - n.y) * 0.0015;
      n.vx *= 0.82; n.vy *= 0.82;
      n.x += n.vx; n.y += n.vy;
      n.x = Math.max(n.r + 4, Math.min(W - n.r - 4, n.x));
      n.y = Math.max(n.r + 4, Math.min(H - n.r - 4, n.y));
    }
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    // связи
    for (const l of links) {
      const a = nodes[l.a], b = nodes[l.b];
      const on = hover >= 0 && (l.a === hover || l.b === hover);
      ctx.strokeStyle = on ? 'rgba(167,139,250,.75)' : 'rgba(255,255,255,.10)';
      ctx.lineWidth = on ? 1.6 : 0.8;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    // узлы
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const t = TYPES[n.type];
      const active = hover < 0 || i === hover || neigh[hover].has(i);
      const r = n.r * (i === hover ? 1.5 : 1);
      const g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 3);
      g.addColorStop(0, `rgba(${t.c},${active ? 0.9 : 0.25})`);
      g.addColorStop(1, `rgba(${t.c},0)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(n.x, n.y, r * 3, 0, 7); ctx.fill();
      ctx.fillStyle = `rgba(${t.c},${active ? 1 : 0.4})`;
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 7); ctx.fill();
    }
    // подпись активного
    if (hover >= 0) {
      const n = nodes[hover], t = TYPES[n.type];
      const txt = `${t.label}: ${n.label}`;
      ctx.font = '600 12px ui-monospace, monospace';
      const w = ctx.measureText(txt).width + 16;
      ctx.fillStyle = 'rgba(10,10,18,.9)';
      ctx.strokeStyle = `rgba(${t.c},.5)`;
      const bx = Math.min(Math.max(n.x - w / 2, 4), W - w - 4), by = n.y - n.r - 30;
      ctx.lineWidth = 1; roundRect(ctx, bx, by, w, 22, 7); ctx.fill(); ctx.stroke();
      ctx.fillStyle = '#eef0ff'; ctx.fillText(txt, bx + 8, by + 15);
    }
  }
  function roundRect(c, x, y, w, h, r) { c.beginPath(); c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath(); }

  let visible = true;
  new IntersectionObserver(([e]) => { visible = e.isIntersecting; }, { threshold: 0 }).observe(canvas);

  if (reduce) { for (let k = 0; k < 220; k++) step(); draw(); return; }

  const FR = 1000 / 30; let last = 0;
  function tick(now) {
    requestAnimationFrame(tick);
    if (!visible || document.hidden) return;
    if (now - last < FR) return; last = now;
    step(); draw();
  }
  requestAnimationFrame(tick);
}
