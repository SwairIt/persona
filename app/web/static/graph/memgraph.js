/* Граф памяти кабинета — реальные данные из /api/graph.json.
   canvas-2D force-directed: фильтр типов узлов + настраиваемая физика
   (отталкивание, длина/жёсткость связей, гравитация, трение). Настройки
   сохраняются в localStorage. Лёгкий, 30 FPS, пауза на скрытой вкладке. */
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

  // --- настройки (с дефолтами) ---
  const PHYS_DEFAULT = {
    repulsion: 1100,   // сила отталкивания узлов (больше — просторнее, меньше слипания)
    linkDist: 70,      // желаемая длина связи
    linkStr: 0.035,    // жёсткость связи
    gravity: 0.011,    // притяжение к центру (держит граф вместе)
    friction: 0.86,    // трение: ближе к 1 = меньше трения, мягче глайд (был 0.82)
  };
  // по умолчанию включено только «нужное»: чаты, промпты, ответы, конспекты
  const SHOW_DEFAULT = {
    prompt: true, answer: true, session: true, summary: true,
    memory: false, recording: false, day: false,
  };
  const MAX_FOCUS = 60;   // фокус-режим: сколько вершин показываем по умолчанию
  // важность для отбора в фокусе: структурные узлы — якоря, потом по свежести
  const TYPE_RANK = { day: 0, session: 1, summary: 2, memory: 3, recording: 4, answer: 5, prompt: 6 };
  const LS = 'persona_graph_cfg_v2';  // v2 — новые дефолты физики + фокус-режим
  let cfg = { phys: { ...PHYS_DEFAULT }, show: { ...SHOW_DEFAULT }, focus: true };
  try {
    const saved = JSON.parse(localStorage.getItem(LS) || 'null');
    if (saved && saved.phys && saved.show) {
      cfg = {
        phys: { ...PHYS_DEFAULT, ...saved.phys },
        show: { ...SHOW_DEFAULT, ...saved.show },
        focus: saved.focus !== false,  // по умолчанию фокус включён
      };
    }
  } catch (e) { /* битый конфиг — дефолты */ }
  const saveCfg = () => { try { localStorage.setItem(LS, JSON.stringify(cfg)); } catch (e) {} };
  const expanded = new Set();  // id вершин, раскрытых вручную (клик «Связи» / поиск)
  function cmpImportance(a, b) {
    const ra = (a.type in TYPE_RANK) ? TYPE_RANK[a.type] : 9;
    const rb = (b.type in TYPE_RANK) ? TYPE_RANK[b.type] : 9;
    if (ra !== rb) return ra - rb;
    return String(b.at || '').localeCompare(String(a.at || ''));  // новее — выше
  }

  let RAW = null;        // сырые данные с сервера
  let sim = null;        // текущее состояние симуляции

  fetch('/api/graph.json', { headers: { 'Accept': 'application/json' } })
    .then((r) => r.json())
    .then((data) => { RAW = data; setupControls(); build(); })
    .catch(() => { if (statsEl) statsEl.textContent = 'Не удалось загрузить граф.'; });

  // размеры canvas
  let W = 0, H = 0; const dpr = Math.min(devicePixelRatio || 1, 2);
  function resize() {
    W = stage.clientWidth || 800; H = stage.clientHeight || 520;
    canvas.width = W * dpr; canvas.height = H * dpr;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  addEventListener('resize', () => { resize(); }, { passive: true });

  function escapeHtml(s) { return (s || '').replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c])); }

  // --- построение видимого графа из RAW по фильтру типов ---
  function build() {
    const rawNodes = (RAW && RAW.nodes) || [];
    if (!rawNodes.length) {
      if (emptyEl) emptyEl.hidden = false;
      if (statsEl) statsEl.textContent = 'Память пока пуста.';
      sim = null; return;
    }
    if (emptyEl) emptyEl.hidden = true;

    // снапшот позиций текущих узлов — чтобы при раскрытии/поиске граф не прыгал
    const prev = new Map();
    const prevQ = sim ? sim.searchQ : '';
    if (sim) sim.nodes.forEach((n) => prev.set(n.id, { x: n.x, y: n.y, vx: n.vx, vy: n.vy }));

    // кандидаты по фильтру типов
    const candidates = rawNodes.filter((n) => cfg.show[n.type]);
    // фокус-режим: показываем важные/недавние + раскрытые вручную; иначе всё
    let visibleIds;
    if (!cfg.focus || candidates.length <= MAX_FOCUS) {
      visibleIds = new Set(candidates.map((n) => n.id));
    } else {
      const ranked = candidates.slice().sort(cmpImportance);
      visibleIds = new Set(ranked.slice(0, MAX_FOCUS).map((n) => n.id));
      expanded.forEach((id) => visibleIds.add(id));
    }
    const shownRaw = candidates.filter((n) => visibleIds.has(n.id));
    const hiddenCount = candidates.length - shownRaw.length;

    const idx = new Map();
    const nodes = [];
    shownRaw.forEach((n) => { idx.set(n.id, nodes.length); nodes.push(makeNode(n)); });
    const links = ((RAW && RAW.links) || [])
      .filter((l) => idx.has(l.a) && idx.has(l.b))
      .map((l) => ({ a: idx.get(l.a), b: idx.get(l.b) }));

    // легенда + статистика (по видимым)
    const counts = {};
    nodes.forEach((n) => { counts[n.type] = (counts[n.type] || 0) + 1; });
    if (statsEl) {
      const parts = [];
      if (counts.prompt) parts.push(counts.prompt + ' промптов');
      if (counts.answer) parts.push(counts.answer + ' ответов');
      if (counts.session) parts.push(counts.session + ' чатов');
      const cards = (counts.memory || 0) + (counts.recording || 0);
      if (cards) parts.push(cards + ' карточек');
      if (counts.day) parts.push(counts.day + ' дней');
      let txt = parts.join(' · ') || 'нет узлов выбранных типов';
      if (hiddenCount > 0) txt += ' · скрыто ' + hiddenCount + ' (клик по узлу → «Связи»)';
      statsEl.textContent = txt + (RAW.truncated ? ' · показаны недавние' : '');
    }
    if (legendEl) {
      legendEl.innerHTML = Object.keys(TYPES).filter((t) => counts[t])
        .map((t) => `<span><i style="background:rgb(${TYPES[t].c})"></i>${TYPES[t].label}</span>`).join('');
    }
    updateShowAll(hiddenCount);

    const neigh = nodes.map(() => new Set());
    links.forEach((l) => { neigh[l.a].add(l.b); neigh[l.b].add(l.a); });

    // раскладка: существующие — на своих местах (без прыжка), новые — по окружности
    let seed = 9; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    const R = Math.min(W, H) * 0.36;
    nodes.forEach((n, i) => {
      const p = prev.get(n.id);
      if (p) { n.x = p.x; n.y = p.y; n.vx = p.vx; n.vy = p.vy; }
      else {
        const a = (i / nodes.length) * Math.PI * 2;
        n.x = W / 2 + Math.cos(a) * R + (rnd() - 0.5) * 50;
        n.y = H / 2 + Math.sin(a) * R + (rnd() - 0.5) * 50;
        n.vx = n.vy = 0;
      }
    });

    sim = { nodes, links, neigh, hover: -1, drag: -1, alpha: 1, searchQ: prevQ || '', searchHits: new Set() };
    computeHits();  // пересчитать подсветку поиска для нового набора узлов
    if (reduce) { for (let k = 0; k < 280; k++) step(); draw(); }  // статичная раскладка
  }

  function makeNode(n) {
    const t = TYPES[n.type] || TYPES.prompt;
    return { ...n, r: t.r, x: 0, y: 0, vx: 0, vy: 0 };
  }

  // --- взаимодействие ---
  const at = (e) => { const r = canvas.getBoundingClientRect(); const t = e.touches ? e.touches[0] : e; return { x: t.clientX - r.left, y: t.clientY - r.top, cx: t.clientX, cy: t.clientY }; };
  const pick = (p) => { if (!sim) return -1; let b = -1, bd = 18 * 18; for (let i = 0; i < sim.nodes.length; i++) { const dx = sim.nodes[i].x - p.x, dy = sim.nodes[i].y - p.y, d = dx * dx + dy * dy; if (d < bd) { bd = d; b = i; } } return b; };
  canvas.style.touchAction = 'none';
  canvas.addEventListener('pointerdown', (e) => {
    if (!sim) return;
    sim.drag = pick(at(e));
    sim.downNode = sim.drag; sim.downX = e.clientX; sim.downY = e.clientY; sim.moved = false;
    if (sim.drag >= 0) { canvas.classList.add('grabbing'); sim.alpha = Math.max(sim.alpha, 0.6); }
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!sim) return;
    const p = at(e);
    if (sim.drag >= 0) {
      if (Math.abs(e.clientX - sim.downX) + Math.abs(e.clientY - sim.downY) > 5) sim.moved = true;
      const n = sim.nodes[sim.drag]; n.x = p.x; n.y = p.y; n.vx = n.vy = 0; sim.alpha = Math.max(sim.alpha, 0.5);
    } else {
      sim.hover = pick(p);
      canvas.style.cursor = sim.hover >= 0 ? 'grab' : 'default';
      if (sim.hover >= 0 && tip) {
        const n = sim.nodes[sim.hover], t = TYPES[n.type] || TYPES.prompt;
        tip.hidden = false;
        tip.innerHTML = `<span class="tt-type">${t.label}${n.compressed ? ' · сжато' : ''}</span>${escapeHtml(n.label || '')}`;
        const sr = stage.getBoundingClientRect();
        const tx = p.cx - sr.left + 14, ty = p.cy - sr.top + 14;
        tip.style.left = Math.min(tx, sr.width - 240) + 'px';
        tip.style.top = Math.min(ty, sr.height - 60) + 'px';
      } else if (tip) tip.hidden = true;
    }
  });
  addEventListener('pointerup', (e) => {
    if (sim) {
      // клик (без перетаскивания): смещение считаем прямо здесь по координатам
      const dx = Math.abs((e.clientX != null ? e.clientX : sim.downX) - sim.downX);
      const dy = Math.abs((e.clientY != null ? e.clientY : sim.downY) - sim.downY);
      if (dx + dy <= 6) {
        if (sim.downNode >= 0) openDetail(sim.nodes[sim.downNode]);
        else if (sim.downNode === -1 && e.target === canvas) closeDetail();
      }
      sim.drag = -1; sim.downNode = -2;
    }
    canvas.classList.remove('grabbing');
  });
  canvas.addEventListener('pointerleave', () => { if (sim && sim.drag < 0) { sim.hover = -1; if (tip) tip.hidden = true; } });

  // --- карточка деталей узла ---
  const detailEl = document.getElementById('mg-detail');
  const dType = document.getElementById('mg-detail-type');
  const dLabel = document.getElementById('mg-detail-label');
  const dMeta = document.getElementById('mg-detail-meta');
  const dFull = document.getElementById('mg-detail-full');
  const dGo = document.getElementById('mg-detail-go');
  const dDay = document.getElementById('mg-detail-day');
  const dX = document.getElementById('mg-detail-x');

  // A4: локальная дата (YYYY-MM-DD) из timestamp узла для перехода на /day/{date}
  function dayFromAt(at) {
    if (!at) return '';
    try {
      const d = new Date(at);
      if (isNaN(d.getTime())) return ('' + at).slice(0, 10);
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const da = String(d.getDate()).padStart(2, '0');
      return `${y}-${m}-${da}`;
    } catch (e) { return ('' + at).slice(0, 10); }
  }
  if (dX) dX.addEventListener('click', closeDetail);

  function fmtTime(s) {
    if (!s) return '';
    const iso = String(s).replace(' ', 'T');
    const d = new Date(iso);
    if (isNaN(d)) return String(s);
    try {
      return d.toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
    } catch (e) { return String(s); }
  }
  const GO_LABEL = {
    prompt: 'Открыть в чате →', answer: 'Открыть в чате →', session: 'Открыть чат →',
    summary: 'Открыть чат →', day: 'Открыть день в хранилище →',
    recording: 'Смотреть записи дня →', memory: 'Смотреть записи дня →',
  };
  function openDetail(n) {
    if (!n || !detailEl) return;
    const t = TYPES[n.type] || TYPES.prompt;
    dType.textContent = t.label + (n.compressed ? ' · сжато' : '');
    dType.style.background = `rgb(${t.c})`;
    dLabel.textContent = n.label || '';
    const when = fmtTime(n.at);
    dMeta.textContent = [n.where || '', when].filter(Boolean).join(' · ');
    dFull.textContent = n.full || '';
    dFull.hidden = !n.full;
    if (n.href) { dGo.hidden = false; dGo.href = n.href; dGo.textContent = GO_LABEL[n.type] || 'Перейти →'; }
    else dGo.hidden = true;
    // A4: «К этому дню» — для любого узла со временем (кроме самих day-узлов,
    // которые и так ведут на день через основной переход).
    const dayStr = n.type === 'day' ? '' : dayFromAt(n.at);
    if (dDay) {
      if (dayStr) { dDay.hidden = false; dDay.href = '/day/' + dayStr; }
      else dDay.hidden = true;
    }
    // «Показать связи» — сколько соседей ещё скрыто (фокус-режим)
    _curNode = n;
    const exp = document.getElementById('mg-detail-expand');
    if (exp) {
      const links = (RAW && RAW.links) || [];
      const shownIds = new Set(sim ? sim.nodes.map((x) => x.id) : []);
      let hiddenNb = 0;
      links.forEach((l) => {
        if (l.a === n.id && !shownIds.has(l.b)) hiddenNb++;
        if (l.b === n.id && !shownIds.has(l.a)) hiddenNb++;
      });
      if (hiddenNb > 0) { exp.hidden = false; exp.textContent = '🔗 Показать связи (+' + hiddenNb + ')'; }
      else exp.hidden = true;
    }
    detailEl.hidden = false;
  }
  function closeDetail() { if (detailEl) detailEl.hidden = true; }

  // --- поиск / фокус-режим / раскрытие связей ---
  let _curNode = null;  // узел, открытый в карточке (для кнопки «Связи»)

  function computeHits() {
    if (!sim) return;
    const q = (sim.searchQ || '').toLowerCase();
    sim.searchHits = new Set();
    if (!q) return;
    sim.nodes.forEach((n, i) => {
      if (((n.label || '') + ' ' + (n.full || '')).toLowerCase().includes(q)) sim.searchHits.add(i);
    });
  }

  function onSearch(q) {
    if (!sim) return;
    sim.searchQ = q;
    computeHits();
    // нет совпадений среди видимых, но есть в полном графе — раскрываем их
    if (q && sim.searchHits.size === 0) {
      const ql = q.toLowerCase();
      const matches = ((RAW && RAW.nodes) || [])
        .filter((n) => cfg.show[n.type] && (((n.label || '') + ' ' + (n.full || '')).toLowerCase().includes(ql)))
        .slice(0, 12);
      if (matches.length) { matches.forEach((n) => expanded.add(n.id)); build(); }
    }
    if (sim) sim.alpha = Math.max(sim.alpha, 0.4);
  }

  function focusFirstHit() {
    if (!sim || !sim.searchHits.size) return;
    const i = sim.searchHits.values().next().value;
    const n = sim.nodes[i];
    n.x = W / 2; n.y = H / 2; n.vx = n.vy = 0;  // подтянуть к центру
    sim.alpha = 1;
    openDetail(n);
  }

  function expandNode(id) {
    if (id == null) return 0;
    const links = (RAW && RAW.links) || [];
    let added = 0;
    links.forEach((l) => {
      if (l.a === id && !expanded.has(l.b)) { expanded.add(l.b); added++; }
      if (l.b === id && !expanded.has(l.a)) { expanded.add(l.a); added++; }
    });
    expanded.add(id);
    build();
    if (sim) sim.alpha = 1;
    return added;
  }

  function updateShowAll(hiddenCount) {
    const b = document.getElementById('mg-show-all');
    if (!b) return;
    if (hiddenCount > 0 && cfg.focus) { b.hidden = false; b.textContent = 'Показать все (+' + hiddenCount + ')'; }
    else b.hidden = true;
  }

  // --- физика (по cfg.phys), мягкие границы вместо жёсткого клампа в углы ---
  function step() {
    if (!sim) return;
    // «остывание»: когда симуляция успокоилась и ничего не тащат — замираем
    // (без этого граф вечно микро-дрожал — те самые «баганые»).
    if (sim.alpha < 0.03 && sim.drag < 0) return;
    const { nodes, links } = sim;
    const P = cfg.phys;
    const cx = W / 2, cy = H / 2;
    const rep = P.repulsion;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y; let d2 = dx * dx + dy * dy || 0.01;
        if (d2 > 160000) continue;            // дальние не считаем (быстро)
        const d = Math.sqrt(d2); const f = rep / d2;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
      }
    }
    for (const l of links) {
      const a = nodes[l.a], b = nodes[l.b];
      let dx = b.x - a.x, dy = b.y - a.y; const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - P.linkDist) * P.linkStr, fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    const VMAX = 30;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i]; if (i === sim.drag) continue;
      n.vx += (cx - n.x) * P.gravity; n.vy += (cy - n.y) * P.gravity;
      n.vx *= P.friction; n.vy *= P.friction;
      // ограничение скорости — не даёт «выстреливать» в углы
      if (n.vx > VMAX) n.vx = VMAX; else if (n.vx < -VMAX) n.vx = -VMAX;
      if (n.vy > VMAX) n.vy = VMAX; else if (n.vy < -VMAX) n.vy = -VMAX;
      n.x += n.vx; n.y += n.vy;
      // мягкий отбой от краёв (не липнем в угол, а отражаемся внутрь)
      const m = n.r + 6;
      if (n.x < m) { n.x = m; n.vx = Math.abs(n.vx) * 0.5; }
      else if (n.x > W - m) { n.x = W - m; n.vx = -Math.abs(n.vx) * 0.5; }
      if (n.y < m) { n.y = m; n.vy = Math.abs(n.vy) * 0.5; }
      else if (n.y > H - m) { n.y = H - m; n.vy = -Math.abs(n.vy) * 0.5; }
    }
    sim.alpha *= 0.985;  // постепенно остываем → плавно встаёт и замирает
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    if (!sim) return;
    const { nodes, links, neigh, hover } = sim;
    for (const l of links) {
      const a = nodes[l.a], b = nodes[l.b];
      const on = hover >= 0 && (l.a === hover || l.b === hover);
      ctx.strokeStyle = on ? 'rgba(167,139,250,.8)' : 'rgba(255,255,255,.07)';
      ctx.lineWidth = on ? 1.5 : 0.6;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    const q = sim.searchQ, hits = sim.searchHits;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i], t = TYPES[n.type] || TYPES.prompt;
      const isHit = q ? hits.has(i) : true;
      const active = hover < 0 || i === hover || neigh[hover].has(i);
      const sdim = (q && !isHit) ? 0.2 : 1;   // при поиске не-совпадения приглушаем
      const dim = (n.compressed ? 0.45 : 1) * sdim;
      const r = n.r * (i === hover ? 1.5 : (q && isHit ? 1.35 : 1));
      const g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r * 3);
      g.addColorStop(0, `rgba(${t.c},${(active ? 0.85 : 0.18) * dim})`);
      g.addColorStop(1, `rgba(${t.c},0)`);
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(n.x, n.y, r * 3, 0, 7); ctx.fill();
      ctx.fillStyle = `rgba(${t.c},${(active ? 1 : 0.35) * dim})`;
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 7); ctx.fill();
      if (q && isHit) {  // кольцо вокруг найденного
        ctx.strokeStyle = 'rgba(94,234,212,0.95)'; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(n.x, n.y, r + 4, 0, 7); ctx.stroke();
      }
    }
  }

  // --- панель управления ---
  function setupControls() {
    // тумблеры типов
    const toggleBox = document.getElementById('mg-types');
    if (toggleBox) {
      toggleBox.innerHTML = Object.keys(TYPES).map((t) =>
        `<label class="mg-chip"><input type="checkbox" data-type="${t}" ${cfg.show[t] ? 'checked' : ''}>` +
        `<i style="background:rgb(${TYPES[t].c})"></i>${TYPES[t].label}</label>`).join('');
      toggleBox.querySelectorAll('input[data-type]').forEach((el) => {
        el.addEventListener('change', () => { cfg.show[el.dataset.type] = el.checked; saveCfg(); build(); });
      });
    }
    // пресеты
    const preset = (show) => { cfg.show = { ...show }; saveCfg(); build(); syncControls(); };
    bind('mg-preset-need', () => preset(SHOW_DEFAULT));
    bind('mg-preset-all', () => preset({ prompt: true, answer: true, session: true, summary: true, memory: true, recording: true, day: true }));
    bind('mg-preset-prompts', () => preset({ prompt: true, answer: false, session: false, summary: false, memory: false, recording: false, day: false }));

    // слайдеры физики
    slider('mg-repulsion', 'repulsion');
    slider('mg-linkdist', 'linkDist');
    slider('mg-linkstr', 'linkStr');
    slider('mg-gravity', 'gravity');
    slider('mg-friction', 'friction');
    bind('mg-phys-reset', () => { cfg.phys = { ...PHYS_DEFAULT }; saveCfg(); syncControls(); if (sim) sim.alpha = 1; });

    // поиск по графу (подсветка + раскрытие скрытых совпадений; Enter — к центру)
    const search = document.getElementById('mg-search');
    if (search) {
      let st = null;
      search.addEventListener('input', () => {
        clearTimeout(st); const v = search.value.trim();
        st = setTimeout(() => onSearch(v), 160);
      });
      search.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); onSearch(search.value.trim()); focusFirstHit(); }
        if (e.key === 'Escape') { search.value = ''; onSearch(''); }
      });
    }
    // объём: «показать все» + переключатель фокус-режима
    bind('mg-show-all', () => { cfg.focus = false; saveCfg(); build(); if (sim) sim.alpha = 1; syncControls(); });
    const focusChk = document.getElementById('mg-focus');
    if (focusChk) focusChk.addEventListener('change', () => {
      cfg.focus = focusChk.checked; if (cfg.focus) expanded.clear();
      saveCfg(); build(); if (sim) sim.alpha = 1;
    });
    // раскрыть связи узла из карточки деталей
    bind('mg-detail-expand', () => {
      if (!_curNode) return;
      const id = _curNode.id; expandNode(id);
      const nn = sim && sim.nodes.find((x) => x.id === id);
      if (nn) openDetail(nn);
    });

    syncControls();
  }

  function slider(id, key) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = cfg.phys[key];
    const out = document.getElementById(id + '-val');
    if (out) out.textContent = cfg.phys[key];
    el.addEventListener('input', () => {
      cfg.phys[key] = parseFloat(el.value); saveCfg();
      if (out) out.textContent = el.value;
      if (sim) sim.alpha = Math.max(sim.alpha, 0.5); // «подогреть» сим после правки
    });
  }
  function bind(id, fn) { const el = document.getElementById(id); if (el) el.addEventListener('click', fn); }
  function syncControls() {
    document.querySelectorAll('#mg-types input[data-type]').forEach((el) => { el.checked = !!cfg.show[el.dataset.type]; });
    const fc = document.getElementById('mg-focus'); if (fc) fc.checked = cfg.focus;
    [['mg-repulsion', 'repulsion'], ['mg-linkdist', 'linkDist'], ['mg-linkstr', 'linkStr'], ['mg-gravity', 'gravity'], ['mg-friction', 'friction']]
      .forEach(([id, key]) => { const el = document.getElementById(id); if (el) { el.value = cfg.phys[key]; const o = document.getElementById(id + '-val'); if (o) o.textContent = cfg.phys[key]; } });
  }

  // панель свернуть/развернуть
  bindPanel();
  function bindPanel() {
    const btn = document.getElementById('mg-panel-toggle');
    const panel = document.getElementById('mg-panel');
    if (btn && panel) btn.addEventListener('click', () => { panel.classList.toggle('open'); });
  }

  // --- цикл ---
  if (reduce) return;  // без анимации: build() уже отрисовал статичную раскладку
  const FR = 1000 / 30; let last = 0;
  function tick(now) {
    requestAnimationFrame(tick);
    if (document.hidden) return;
    if (now - last < FR) return; last = now;
    step(); draw();
  }
  requestAnimationFrame(tick);
})();
