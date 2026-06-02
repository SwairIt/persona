/**
 * Persona bookmarklet — captures the current page (URL + title + selection)
 * into a Persona note via /api/bookmarklet/capture.
 *
 * The bookmarklet itself is the IIFE below, wrapped into a `javascript:` URL
 * by the /bookmarklet template. This file is also served as-is at
 * /static/bookmarklet_source.js so users can audit the source before
 * dragging the link to their bookmarks bar.
 *
 * PERSONA_ORIGIN is replaced by the template at render time so the bookmark
 * always points at the host the user clicked "install" on (handles 127.0.0.1
 * vs. localhost vs. a LAN IP without forcing a config edit).
 */
(function () {
  var origin = 'PERSONA_ORIGIN';
  var selection = '';
  try {
    selection = String(window.getSelection ? window.getSelection().toString() : '');
  } catch (e) { selection = ''; }
  // Server also clamps, but trimming here keeps the POST body small.
  if (selection.length > 5000) selection = selection.slice(0, 5000);

  var payload = {
    url: String(window.location.href || ''),
    title: String(document.title || ''),
    selection: selection
  };

  function flash(text, ok) {
    try {
      var el = document.createElement('div');
      el.textContent = text;
      el.style.cssText = [
        'position:fixed', 'top:16px', 'right:16px', 'z-index:2147483647',
        'padding:10px 14px', 'border-radius:8px',
        'font:13px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif',
        'color:#fff',
        'background:' + (ok ? '#7c3aed' : '#dc2626'),
        'box-shadow:0 4px 12px rgba(0,0,0,.25)'
      ].join(';');
      document.body.appendChild(el);
      setTimeout(function () {
        if (el && el.parentNode) el.parentNode.removeChild(el);
      }, 2000);
    } catch (e) { /* DOM unavailable — silent */ }
  }

  fetch(origin + '/api/bookmarklet/capture', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(function (r) {
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }).then(function (data) {
    flash('Saved to Persona (#' + (data && data.note_id) + ')', true);
  }).catch(function (err) {
    flash('Persona save failed: ' + (err && err.message ? err.message : 'error'), false);
  });
})();
