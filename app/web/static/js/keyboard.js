/**
 * Global keyboard shortcuts for Persona.
 *
 *   /        focus the search box (on /search) or jump to it
 *   g        followed by t = timeline, s = search, h = stats, n = settings
 *   j / k    next / previous screenshot card on timeline + search
 *   o        open focused card
 *   Esc      close lightbox / blur input
 *   ?        show shortcut help
 */
(function () {
  let waitingForG = false;
  let waitingTimer = null;
  let focusedIndex = -1;

  function selectors() {
    return Array.from(document.querySelectorAll('[data-shot-card]'));
  }

  function setFocus(idx) {
    const cards = selectors();
    if (cards.length === 0) return;
    if (focusedIndex >= 0 && cards[focusedIndex]) {
      cards[focusedIndex].classList.remove('ring-2', 'ring-accent-500');
    }
    focusedIndex = ((idx % cards.length) + cards.length) % cards.length;
    const target = cards[focusedIndex];
    target.classList.add('ring-2', 'ring-accent-500');
    target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function openFocused() {
    const cards = selectors();
    if (focusedIndex >= 0 && cards[focusedIndex]) {
      window.location.href = cards[focusedIndex].href || cards[focusedIndex].getAttribute('data-href');
    }
  }

  function isEditable(target) {
    if (!target) return false;
    const tag = (target.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || target.isContentEditable;
  }

  function showHelp() {
    let panel = document.getElementById('shortcuts-help');
    if (panel) {
      panel.remove();
      return;
    }
    panel = document.createElement('div');
    panel.id = 'shortcuts-help';
    panel.className =
      'fixed bottom-6 right-6 bg-ink-800 border border-ink-700 rounded-lg p-4 text-sm shadow-xl z-50 max-w-sm';
    panel.innerHTML = `
      <div class="font-semibold mb-2 flex items-center justify-between">
        <span>Keyboard shortcuts</span>
        <button onclick="document.getElementById('shortcuts-help').remove()" class="text-zinc-500 hover:text-zinc-200">×</button>
      </div>
      <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt class="font-mono text-accent-400">/</dt><dd>focus search</dd>
        <dt class="font-mono text-accent-400">g t</dt><dd>timeline</dd>
        <dt class="font-mono text-accent-400">g s</dt><dd>search</dd>
        <dt class="font-mono text-accent-400">g h</dt><dd>stats</dd>
        <dt class="font-mono text-accent-400">g n</dt><dd>settings</dd>
        <dt class="font-mono text-accent-400">j / k</dt><dd>next / prev card</dd>
        <dt class="font-mono text-accent-400">o / Enter</dt><dd>open focused</dd>
        <dt class="font-mono text-accent-400">Esc</dt><dd>close / blur</dd>
        <dt class="font-mono text-accent-400">?</dt><dd>this panel</dd>
      </dl>
    `;
    document.body.appendChild(panel);
  }

  function handleG(key) {
    waitingForG = false;
    clearTimeout(waitingTimer);
    switch (key) {
      case 't': window.location.href = '/'; break;
      case 's': window.location.href = '/search'; break;
      case 'h': window.location.href = '/stats'; break;
      case 'n': window.location.href = '/settings'; break;
    }
  }

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') {
      if (document.activeElement && isEditable(document.activeElement)) {
        document.activeElement.blur();
        return;
      }
      const lightbox = document.getElementById('lightbox');
      if (lightbox) lightbox.remove();
      const help = document.getElementById('shortcuts-help');
      if (help) help.remove();
      return;
    }

    if (isEditable(event.target)) return;

    if (waitingForG) {
      handleG(event.key.toLowerCase());
      return;
    }

    switch (event.key) {
      case '/': {
        event.preventDefault();
        const searchInput = document.querySelector('input[name="q"]');
        if (searchInput) searchInput.focus();
        else window.location.href = '/search';
        break;
      }
      case 'g':
        waitingForG = true;
        waitingTimer = setTimeout(() => (waitingForG = false), 1200);
        break;
      case 'j':
        setFocus(focusedIndex + 1);
        break;
      case 'k':
        setFocus(focusedIndex - 1);
        break;
      case 'o':
      case 'Enter':
        openFocused();
        break;
      case '?':
        showHelp();
        break;
    }
  });

  document.addEventListener('click', function (event) {
    const trigger = event.target.closest('[data-lightbox-src]');
    if (!trigger) return;
    event.preventDefault();
    const src = trigger.getAttribute('data-lightbox-src');
    openLightbox(src);
  });

  function openLightbox(src) {
    const existing = document.getElementById('lightbox');
    if (existing) existing.remove();
    const node = document.createElement('div');
    node.id = 'lightbox';
    node.className =
      'fixed inset-0 bg-black/90 z-50 flex items-center justify-center p-6 cursor-zoom-out';
    node.innerHTML = `<img src="${src}" class="max-w-full max-h-full object-contain rounded shadow-2xl">`;
    node.addEventListener('click', () => node.remove());
    document.body.appendChild(node);
  }
})();
