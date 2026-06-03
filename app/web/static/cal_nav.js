/*
 * cal_nav.js — global floating month navigator (v0.59)
 *
 * Renders a tiny bottom-right calendar that lets the user jump to any
 * day's /timeline/{YYYY-MM-DD} page in one click.  The widget mounts
 * itself on DOMContentLoaded so any page that pulls base.html gets it
 * for free.  No build step, no framework dependency — vanilla ES2020.
 *
 * Data source: GET /api/cal-nav-days.json?month=YYYY-MM
 *   Response: { month, first_day, last_day, days: [{ date, count }] }
 *
 * Behaviour:
 *   - Click the floating FAB to toggle the panel.
 *   - Arrows in the header paginate months; the server is hit once per
 *     month and the result is cached for the lifetime of the page.
 *   - Days with shots are tinted (4 heat levels by relative count).
 *   - Clicking a date navigates to /timeline/{YYYY-MM-DD}.
 *   - Esc closes the panel.  Click-outside also closes.
 */
(function () {
  'use strict';

  // Guard: never mount twice (some pages re-run base.html scripts after
  // an htmx swap, and we don't want two FABs stacked on top of each other).
  if (window.__personaCalNavMounted) return;
  window.__personaCalNavMounted = true;

  /** @typedef {{ date: string, count: number }} CalNavDay */

  /** @type {Map<string, { days: CalNavDay[], byDate: Map<string, number>, max: number }>} */
  const cache = new Map();

  const WEEKDAY_LABELS = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];
  const MONTH_LABELS = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'
  ];

  /** Format a Date as YYYY-MM-DD in local time (matches DATE() in SQL). */
  function ymd(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  /** Format a Date as YYYY-MM. */
  function ym(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    return `${y}-${m}`;
  }

  /**
   * Fetch the per-day counts for a given YYYY-MM.  Results are memoised
   * for the lifetime of the page so flipping back and forth between
   * months never re-hits the server.
   */
  async function loadMonth(monthKey) {
    const cached = cache.get(monthKey);
    if (cached) return cached;

    const url = `/api/cal-nav-days.json?month=${encodeURIComponent(monthKey)}`;
    let payload = { days: [] };
    try {
      const res = await fetch(url, { credentials: 'same-origin' });
      if (res.ok) {
        payload = await res.json();
      }
    } catch (e) {
      // Silent: an offline or 500 just means "no highlights this month".
    }

    /** @type {CalNavDay[]} */
    const days = Array.isArray(payload.days) ? payload.days : [];
    const byDate = new Map();
    let max = 0;
    for (const d of days) {
      if (!d || typeof d.date !== 'string') continue;
      const n = Number(d.count) || 0;
      byDate.set(d.date, n);
      if (n > max) max = n;
    }

    const entry = { days, byDate, max };
    cache.set(monthKey, entry);
    return entry;
  }

  /**
   * Map a count to a heat bucket 1..4 relative to the busiest day in
   * the visible month.  Zero counts stay un-highlighted.
   */
  function heatBucket(count, max) {
    if (!count || !max) return 0;
    const ratio = count / max;
    if (ratio >= 0.75) return 4;
    if (ratio >= 0.5) return 3;
    if (ratio >= 0.25) return 2;
    return 1;
  }

  function makeEl(tag, attrs, children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (v === false || v == null) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    if (children) {
      for (const c of children) {
        if (c == null) continue;
        el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      }
    }
    return el;
  }

  /**
   * Build the calendar grid for a given (year, month) into the supplied
   * container.  Month is 0-indexed to match JS Date.
   */
  async function renderGrid(container, year, month) {
    const monthKey = `${year}-${String(month + 1).padStart(2, '0')}`;
    container.innerHTML = '';
    container.appendChild(makeEl('div', { class: 'cal-nav-empty', text: 'Loading…' }));

    const entry = await loadMonth(monthKey);
    container.innerHTML = '';

    const grid = makeEl('div', { class: 'cal-nav-grid', role: 'grid' });

    const firstOfMonth = new Date(year, month, 1);
    // JS getDay(): 0=Sun..6=Sat. We use Mon-first → shift so Mon=0..Sun=6.
    const leading = (firstOfMonth.getDay() + 6) % 7;
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const todayIso = ymd(new Date());

    for (let i = 0; i < leading; i++) {
      const filler = makeEl('button', { class: 'cal-nav-cell', disabled: true, tabindex: '-1' });
      grid.appendChild(filler);
    }

    for (let day = 1; day <= daysInMonth; day++) {
      const iso = `${monthKey}-${String(day).padStart(2, '0')}`;
      const count = entry.byDate.get(iso) || 0;
      const bucket = heatBucket(count, entry.max);
      const cell = makeEl('button', {
        class: 'cal-nav-cell',
        type: 'button',
        'data-date': iso,
        'data-has': count > 0 ? '1' : '0',
        'data-heat': String(bucket),
        'data-today': iso === todayIso ? '1' : '0',
        title: count > 0 ? `${iso} — ${count} shot${count === 1 ? '' : 's'}` : iso,
      }, [String(day)]);
      cell.addEventListener('click', () => {
        window.location.assign(`/timeline/${iso}`);
      });
      grid.appendChild(cell);
    }

    container.appendChild(grid);
  }

  function mount() {
    const root = makeEl('div', { class: 'cal-nav-root', 'data-cal-nav-open': '0' });

    const fab = makeEl('button', {
      class: 'cal-nav-fab',
      type: 'button',
      title: 'Calendar (jump to day)',
      'aria-label': 'Open calendar navigator',
      'aria-expanded': 'false',
    }, ['▦']);

    const panel = makeEl('div', { class: 'cal-nav-panel', role: 'dialog', 'aria-label': 'Calendar' });

    const titleEl = makeEl('div', { class: 'cal-nav-title' });
    const prevBtn = makeEl('button', { class: 'cal-nav-btn', type: 'button', 'aria-label': 'Previous month' }, ['‹']);
    const nextBtn = makeEl('button', { class: 'cal-nav-btn', type: 'button', 'aria-label': 'Next month' }, ['›']);

    const header = makeEl('div', { class: 'cal-nav-header' }, [
      titleEl,
      makeEl('div', { class: 'cal-nav-nav' }, [prevBtn, nextBtn]),
    ]);

    const weekdays = makeEl('div', { class: 'cal-nav-weekdays' });
    for (const w of WEEKDAY_LABELS) weekdays.appendChild(makeEl('span', { text: w }));

    const gridHost = makeEl('div', { class: 'cal-nav-grid-host' });

    const todayBtn = makeEl('button', {
      class: 'cal-nav-today-btn',
      type: 'button',
      title: 'Jump to today',
    }, ['Today']);
    const footer = makeEl('div', { class: 'cal-nav-footer' }, [
      makeEl('span', { text: 'Click a day to open it' }),
      todayBtn,
    ]);

    panel.appendChild(header);
    panel.appendChild(weekdays);
    panel.appendChild(gridHost);
    panel.appendChild(footer);

    root.appendChild(fab);
    root.appendChild(panel);
    document.body.appendChild(root);

    // Visible month state — start on the current month.
    const now = new Date();
    let viewYear = now.getFullYear();
    let viewMonth = now.getMonth(); // 0-indexed

    function refresh() {
      titleEl.textContent = `${MONTH_LABELS[viewMonth]} ${viewYear}`;
      renderGrid(gridHost, viewYear, viewMonth);
    }

    function setOpen(open) {
      root.setAttribute('data-cal-nav-open', open ? '1' : '0');
      fab.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        // Re-sync to "now" whenever the user re-opens — the page may
        // have been left open across midnight.
        const fresh = new Date();
        if (fresh.getFullYear() !== viewYear || fresh.getMonth() !== viewMonth) {
          viewYear = fresh.getFullYear();
          viewMonth = fresh.getMonth();
        }
        refresh();
      }
    }

    fab.addEventListener('click', (e) => {
      e.stopPropagation();
      setOpen(root.getAttribute('data-cal-nav-open') !== '1');
    });

    prevBtn.addEventListener('click', () => {
      if (viewMonth === 0) { viewMonth = 11; viewYear--; } else { viewMonth--; }
      refresh();
    });

    nextBtn.addEventListener('click', () => {
      if (viewMonth === 11) { viewMonth = 0; viewYear++; } else { viewMonth++; }
      refresh();
    });

    todayBtn.addEventListener('click', () => {
      window.location.assign(`/timeline/${ymd(new Date())}`);
    });

    // Esc closes; click-outside closes too.
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && root.getAttribute('data-cal-nav-open') === '1') {
        setOpen(false);
      }
    });
    document.addEventListener('click', (e) => {
      if (root.getAttribute('data-cal-nav-open') !== '1') return;
      if (root.contains(/** @type {Node} */ (e.target))) return;
      setOpen(false);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount, { once: true });
  } else {
    mount();
  }
})();
