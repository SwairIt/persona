/* Граф памяти кабинета — реальные данные из /api/graph.json.
   canvas-2D force-directed, перетаскивание узлов, наведение → подсказка,
   подсветка связей. Лёгкий (≈350 узлов max), 30 FPS, пауза на скрытой вкладке. */
(function () {
  'use strict';
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const canvas = document.getElementById('mg-canvas');
  const stage = document.getElementById('mg-stage');
  const tip = document.getElementById('mg-tip');
  const statsEl = document.getElementById('mg-stats');
  const legendEl = document.getElementById('mg-legend');
  const emptyEl = document.getElementById('mg-empty');
  if (!canvas || !stage) return;
  const ctx = canvas.getContext('2d');

  const TYPES = {
    prompt:    { c: '167,139,250', r: 5.5, label: 'промпт' },
    answer:    { c: '94,234,212',  r: 5,   label: 'ответ' },
    memory:    { c: '244,114,182', r: 6,   label: 'память' },
    recording: { c: '251,191,36',  r: 6,   label: 'запись (речь)' },
    day:       { c: '96,165,250',  r: 9,   label: 'день' },
    session:   { c: '240,171,252', r: 8,   label: 'чат' },
    summary:   { c: '252,165,165', r: 7,   label: 'конспект (сжато)' },
  };

  fetch('/api/graph.json', { headers: { 'Accept': 'application/json' } })
    .then((r) => r.json())
    .then(init)
    .catch(() => { if (statsEl) statsEl.textContent = 'Не удалось загрузить граф.'; });

  function init(data) {
    const rawNodes = data.nodes || [];
    if (!rawNodes.length) { if (emptyEl) emptyEl.hidden = false; if (statsEl) statsEl.textContent = 'Память пока пуста.'; return; }

    // легенда + статистика
    const counts = data.counts || {};
    if (statsEl) {
      const parts = [];
      if (counts.prompt) parts.push(counts.prompt + ' промптов');
      if (counts.answer) parts.push(counts.answer + ' ответов');
      if (counts.memory || counts.recording) parts.push(((counts.memory || 0) + (counts.recording || 0)) + ' карточек памяти');
      if (counts.session) parts.push(counts.session + ' чатов');
      statsEl.textContent = parts.join(' · ') + (data.truncated ? ' · показаны недавние' : '');
    }
    if (legendEl) {
      legendEl.innerHTML = Object.keys(TYPES)
        .filter((t) => counts[t])
        .map((t) => `<span><i style="background:rgb(${TYPES[t].c})"></i>${TYPES[t].label}</span>`)
        .join('');
    }

    const idx = new Map();
    const nodes = rawNodes.map((n, i) => {
      idx.set(n.id, i);
      const t = TYPES[n.type] || TYPES.prompt;
      return { ...n, r: t.r, x: 0, y: 0, vx: 0, vy: 0 };
    });
    const links = (data.links || []).filter((l) => idx.has(l.a) && idx.has(l.b))
      .map((l) => ({ a: idx.get(l.a), b: idx.get(l.b) }));
    const neigh = nodes.map(() => new Set());
    links.forEach((l) => { neigh[l.a].add(l.b); neigh[l.b].add(l.a); });

    // размеры
    let W = 0, H = 0; const dpr = Math.min(devicePixelRatio || 1, 2);
    function resize() {
      W = stage.clientWidth; H = stage.clientHeight;
      canvas.width = W * dpr; canvas.height = H * dpr;
      canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    resize();
    addEventListener('resize', resize, { passive: true });

    // стартовая раскладка
    let seed = 9; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    nodes.forEach((n, i) => {
      const a = (i / nodes.length) * Math.PI * 2;
      n.x = W / 2 + Math.cos(a) * Math.min(W, H) * 0.35 + (rnd() - 0.5) * 60;
      n.y = H / 2 + Math.sin(a) * Math.min(W, H) * 0.35 + (rnd() - 0.5) * 60;
    });

    // взаимодействие
    let hover = -1, drag = -1;
    const at = (e) => { const r = canvas.getBoundingClientRect(); const t = e.touches ? e.touches[0] : e; return { x: t.clientX - r.left, y: t.clientY - r.top, cx: t.clientX, cy: t.clientY }; };
    const pick = (p) => { let b = -1, bd = 16 * 16; for (let i = 0; i < nodes.length; i++) { const dx = nodes[i].x - p.x, dy = nodes[i].y - p.y, d = dx * dx + dy * dy; if (d < bd) { bd = d; b = i; } } return b; };
    canvas.style.touchAction = 'none';
    canvas.addEventListener('pointerdown', (e) => { drag = pick(at(e)); if (drag >= 0) canvas.classList.add('grabbing'); });
    canvas.addEventListener('pointermove', (e) => {
      const p = at(e);
      if (drag >= 0) { nodes[drag].x = p.x; nodes[drag].y = p.y; nodes[drag].vx = nodes[drag].vy = 0; }
      else {
        hover = pick(p);
        canvas.style.cursor = hover >= 0 ? 'grab' : 'default';
        if (hover >= 0 && tip) {
          const n = nodes[hover], t = TYPES[n.type] || TYPES.prompt;
          tip.hidden = false;
          tip.innerHTML = `<span class="tt-type">${t.label}${n.compressed ? ' · сжато' : ''}</span>${escapeHtml(n.label || '')}`;
          const sr = stage.getBoundingClientRect();
          let tx = p.cx - sr.left + 14, ty = p.cy - sr.top + 14;
          tip.style.left = Math.min(tx, sr.width - 240) + 'px';
          tip.style.top = Math.min(ty, sr.height - 60) + 'px';
        } else if (tip) tip.hidden = true;
      }
    });
    addEventListener('pointerup', () => { drag = -1; canvas.classList.remove('grabbing'); });
    canvas.addEventListener('pointerleave', () => { if (drag < 0) { hover = -1; if (tip) tip.hidden = true; } });

    function escapeHtml(s) { return s.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c])); }

    function step() {
      const cx = W / 2, cy = H / 2;
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          let dx = a.x - b.x, dy = a.y - b.y; let d2 = dx * dx + dy * dy || 0.01;
          if (d2 > 90000) continue; // дальние не отталкиваем (быстрее)
          const f = 700 / d2, d = Math.sqrt(d2);
          const fx = (dx / d) * f, fy = (dy / d) * f;
          a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
        }
      }
      for (const l of links) {
        const a = nodes[l.a], b = nodes[l.b];
        let dx = b.x - a.x, dy = b.y - a.y; const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const f = (d - 60) * 0.02, fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i]; if (i === drag) continue;
        n.vx += (cx - n.x) * 0.002; n.vy += (cy - n.y) * 0.002;
        n.vx *= 0.8; n.vy *= 0.8; n.x += n.vx; n.y += n.vy;
        n.x = Math.max(n.r + 3, Math.min(W - n.r - 3, n.x));
        n.y = Math.max(n.r + 3, Math.min(H - n.r - 3, n.y));
      }
    }

    function draw() {
      ctx.clearRect(0, 0, W, H);
      for (const l of links) {
        const a = nodes[l.a], b = nodes[l.b];
        const on = hover >= 0 && (l.a === hover || l.b === hover);
        ctx.strokeStyle = on ? 'rgba(167,139,250,.8)' : 'rgba(255,255,255,.07)';
        ctx.lineWidth = on ? 1.5 : 0.6;
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i], t = TYPES[n.type] || TYPES.prompt;
        const active = hover < 0 || i === hover || neigh[hover].has(i);
        const dim = n.compressed ? 0.45 : 1;
        const r = n.r * (i === hover ? 1.5 : 1);
        const g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 3);
        g.addColorStop(0, `rgba(${t.c},${(active ? 0.85 : 0.18) * dim})`);
        g.addColorStop(1, `rgba(${t.c},0)`);
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(n.x, n.y, r * 3, 0, 7); ctx.fill();
        ctx.fillStyle = `rgba(${t.c},${(active ? 1 : 0.35) * dim})`;
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 7); ctx.fill();
      }
    }

    if (reduce) { for (let k = 0; k < 260; k++) step(); draw(); return; }
    const FR = 1000 / 30; let last = 0;
    function tick(now) {
      requestAnimationFrame(tick);
      if (document.hidden) return;
      if (now - last < FR) return; last = now;
      step(); draw();
    }
    requestAnimationFrame(tick);
  }
})();
