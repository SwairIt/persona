/**
 * Render markdown inside [data-md-source] -> [data-md-target] pairs.
 * Lazy: waits for markdown-it to load.
 */
(function () {
  function render() {
    if (typeof window.markdownit !== 'function') return false;
    const md = window.markdownit({ html: false, linkify: true, breaks: true });
    document.querySelectorAll('[data-md-target]').forEach((target) => {
      const sourceId = target.getAttribute('data-md-source');
      const source = document.getElementById(sourceId);
      if (!source) return;
      const text = source.value || source.textContent || '';
      target.innerHTML = md.render(text.trim());
    });
    return true;
  }

  function waitAndRender() {
    if (render()) return;
    let tries = 20;
    const id = setInterval(() => {
      if (render() || --tries <= 0) clearInterval(id);
    }, 200);
  }

  document.addEventListener('DOMContentLoaded', waitAndRender);
  document.addEventListener('htmx:afterSwap', waitAndRender);
})();
