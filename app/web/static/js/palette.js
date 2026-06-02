/**
 * Cmd+K / Ctrl+K command palette — fuzzy navigation.
 */
(function () {
  const ROUTES = [
    { label: 'Timeline (today)', href: '/' },
    { label: 'Calendar', href: '/calendar' },
    { label: 'Search', href: '/search' },
    { label: 'Ask (AI Q&A)', href: '/ask' },
    { label: 'Topics', href: '/topics' },
    { label: 'Time-sheet', href: '/timesheet' },
    { label: 'Digest — weekly', href: '/digest/weekly' },
    { label: 'Digest — daily archive', href: '/digest/daily' },
    { label: 'Daily AI summary', href: '/summary/' },
    { label: 'Journal', href: '/journal' },
    { label: 'Apps', href: '/apps' },
    { label: 'Tags & saved searches', href: '/tags' },
    { label: 'Stats', href: '/stats' },
    { label: 'Settings', href: '/settings' },
    { label: 'Whitelist (process deny list)', href: '/whitelist' },
    { label: 'Help', href: '/help' },
    { label: 'Welcome', href: '/welcome' },
    { label: 'Health endpoint', href: '/health' },
  ];

  let overlay = null;
  let input = null;
  let listEl = null;
  let focused = 0;
  let visible = [];

  function fuzzyScore(needle, haystack) {
    needle = needle.toLowerCase();
    haystack = haystack.toLowerCase();
    if (!needle) return 1;
    if (haystack.includes(needle)) return 100 - haystack.indexOf(needle);
    let j = 0;
    for (const ch of needle) {
      while (j < haystack.length && haystack[j] !== ch) j++;
      if (j >= haystack.length) return 0;
      j++;
    }
    return 50 - (haystack.length - needle.length);
  }

  function render() {
    const q = input.value;
    visible = ROUTES
      .map((r) => ({ ...r, score: fuzzyScore(q, r.label) }))
      .filter((r) => r.score > 0)
      .sort((a, b) => b.score - a.score);
    if (focused >= visible.length) focused = 0;
    let html = '';
    visible.forEach((r, idx) => {
      const cls = idx === focused
        ? 'bg-accent-600/30 text-accent-200'
        : 'text-zinc-300 hover:bg-ink-800';
      html += `<a href="${r.href}" data-idx="${idx}" class="block px-4 py-2 rounded ${cls}">${r.label}</a>`;
    });
    if (!visible.length) {
      html = '<div class="px-4 py-2 text-zinc-500 text-sm">No match.</div>';
    }
    listEl.innerHTML = html;
  }

  function open() {
    if (overlay) return;
    overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 bg-black/50 z-50 flex items-start justify-center pt-24 px-4';
    overlay.innerHTML = `
      <div class="bg-ink-900 border border-ink-700 rounded-lg shadow-2xl w-full max-w-xl overflow-hidden">
        <input type="text" placeholder="Where to?" id="palette-input"
               class="w-full px-4 py-3 bg-transparent border-0 border-b border-ink-700 text-zinc-100 focus:outline-none">
        <div id="palette-list" class="max-h-80 overflow-y-auto p-2 text-sm"></div>
        <div class="px-4 py-2 border-t border-ink-700 flex items-center justify-between text-xs text-zinc-500">
          <span>↑↓ navigate · Enter open · Esc close</span>
        </div>
      </div>
    `;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.body.appendChild(overlay);
    input = document.getElementById('palette-input');
    listEl = document.getElementById('palette-list');
    focused = 0;
    input.addEventListener('input', () => { focused = 0; render(); });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); focused = Math.min(visible.length - 1, focused + 1); render(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); focused = Math.max(0, focused - 1); render(); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        if (visible[focused]) window.location.href = visible[focused].href;
      } else if (e.key === 'Escape') {
        close();
      }
    });
    render();
    input.focus();
  }

  function close() {
    if (!overlay) return;
    overlay.remove();
    overlay = null;
    input = null;
    listEl = null;
  }

  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      open();
    }
  });
})();
