/*
 * timeline_scrubber — vanilla JS, no framework.
 *
 * Powers the floating thumbnail tooltip that pops up while hovering a
 * position on the day-timeline scrubber bar. The bar is identified by a
 * `data-scrubber-day="YYYY-MM-DD"` attribute on its outer element (SVG,
 * <input type=range>, or wrapping <div>) — that single attribute is both
 * the activation hook and the carrier of the date to query.
 *
 * Lookup is delegated to GET /api/timeline/preview-at?day=...&hhmm=...
 * which returns {shot_id, captured_at, thumbnail_url, app_name,
 * window_title} or 404 when no capture sits within +/-5 min of the
 * hovered point. Misses are cached too so we don't re-bombard the
 * server on a quiet hour of the day.
 *
 * Self-contained — no Alpine, no htmx. Silent no-op when no bar is on
 * the current page, so the script is safe to ship globally via base.html.
 */
(function () {
  'use strict';

  var DEBOUNCE_MS = 80;
  var FETCH_TIMEOUT_MS = 4000;

  function onReady(fn) {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', fn);
    } else {
      fn();
    }
  }

  onReady(function () {
    var bars = document.querySelectorAll('[data-scrubber-day]');
    if (!bars || bars.length === 0) return;
    bars.forEach(attachToBar);
  });

  function attachToBar(barEl) {
    var day = barEl.getAttribute('data-scrubber-day');
    if (!day) return;

    // Hover-keyed cache. Hit payloads are stored as objects; misses as
    // the literal string 'MISS' so we can distinguish them from
    // "not yet asked".
    var cache = Object.create(null);
    var tooltipEl = null;
    var debounceTimer = null;
    var lastHhmm = null;
    var inflightAbort = null;

    barEl.addEventListener('mousemove', function (ev) {
      var hhmm = computeHhmm(barEl, ev.clientX);
      if (hhmm === null || hhmm === lastHhmm) {
        if (tooltipEl) positionTooltip(tooltipEl, ev);
        return;
      }
      lastHhmm = hhmm;

      if (debounceTimer !== null) {
        clearTimeout(debounceTimer);
      }
      debounceTimer = setTimeout(function () {
        debounceTimer = null;
        handleHover(hhmm, ev);
      }, DEBOUNCE_MS);
    });

    barEl.addEventListener('mouseleave', function () {
      if (debounceTimer !== null) {
        clearTimeout(debounceTimer);
        debounceTimer = null;
      }
      if (inflightAbort) {
        try { inflightAbort.abort(); } catch (e) { /* ignore */ }
        inflightAbort = null;
      }
      lastHhmm = null;
      removeTooltip();
    });

    function handleHover(hhmm, ev) {
      var cached = cache[hhmm];
      if (cached === 'MISS') {
        removeTooltip();
        return;
      }
      if (cached) {
        showTooltip(cached, ev);
        return;
      }
      fetchPreview(hhmm).then(function (data) {
        // The user may have moved on by the time the request resolves;
        // only render if this hhmm is still the active hover target.
        if (lastHhmm !== hhmm) return;
        if (data === null) {
          cache[hhmm] = 'MISS';
          removeTooltip();
          return;
        }
        cache[hhmm] = data;
        showTooltip(data, ev);
      });
    }

    function fetchPreview(hhmm) {
      if (inflightAbort) {
        try { inflightAbort.abort(); } catch (e) { /* ignore */ }
      }
      var controller = null;
      try {
        controller = new AbortController();
      } catch (e) {
        controller = null;
      }
      inflightAbort = controller;
      var timeoutId = null;
      if (controller) {
        timeoutId = setTimeout(function () {
          try { controller.abort(); } catch (e) { /* ignore */ }
        }, FETCH_TIMEOUT_MS);
      }
      var url = '/api/timeline/preview-at?day=' +
        encodeURIComponent(day) + '&hhmm=' + encodeURIComponent(hhmm);
      var opts = controller ? { signal: controller.signal } : {};
      return fetch(url, opts).then(function (r) {
        if (timeoutId !== null) clearTimeout(timeoutId);
        if (r.status === 404) return null;
        if (!r.ok) return null;
        return r.json();
      }).catch(function () {
        if (timeoutId !== null) clearTimeout(timeoutId);
        return null;
      });
    }

    function showTooltip(data, ev) {
      if (!tooltipEl) {
        tooltipEl = buildTooltip();
        document.body.appendChild(tooltipEl);
      }
      var img = tooltipEl.querySelector('img');
      var meta = tooltipEl.querySelector('.persona-tlp-meta');
      if (img && img.getAttribute('src') !== data.thumbnail_url) {
        img.setAttribute('src', data.thumbnail_url);
      }
      if (meta) {
        meta.textContent = buildMetaText(data);
      }
      positionTooltip(tooltipEl, ev);
    }

    function removeTooltip() {
      if (tooltipEl && tooltipEl.parentNode) {
        tooltipEl.parentNode.removeChild(tooltipEl);
      }
      tooltipEl = null;
    }
  }

  function buildTooltip() {
    var box = document.createElement('div');
    box.className = 'persona-timeline-preview';
    box.setAttribute('role', 'tooltip');
    box.style.position = 'absolute';
    box.style.zIndex = '9999';
    box.style.pointerEvents = 'none';
    box.style.background = 'rgba(17, 17, 20, 0.95)';
    box.style.border = '1px solid #26262e';
    box.style.borderRadius = '6px';
    box.style.padding = '6px';
    box.style.boxShadow = '0 4px 18px rgba(0, 0, 0, 0.4)';
    box.style.color = '#e4e4e7';
    box.style.font = '11px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
    box.style.maxWidth = '240px';

    var img = document.createElement('img');
    img.alt = 'Timeline preview';
    img.style.width = '220px';
    img.style.height = 'auto';
    img.style.display = 'block';
    img.style.borderRadius = '4px';
    box.appendChild(img);

    var meta = document.createElement('div');
    meta.className = 'persona-tlp-meta';
    meta.style.marginTop = '4px';
    meta.style.fontFamily = '"JetBrains Mono", "Cascadia Code", Consolas, monospace';
    meta.style.color = '#a1a1aa';
    meta.style.overflow = 'hidden';
    meta.style.textOverflow = 'ellipsis';
    meta.style.whiteSpace = 'nowrap';
    box.appendChild(meta);

    return box;
  }

  function buildMetaText(data) {
    var bits = [];
    var t = formatTime(data.captured_at);
    if (t) bits.push(t);
    if (data.app_name) bits.push(data.app_name);
    return bits.join(' · ');
  }

  function formatTime(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return '';
      var hh = String(d.getHours()).padStart(2, '0');
      var mm = String(d.getMinutes()).padStart(2, '0');
      return hh + ':' + mm;
    } catch (e) {
      return '';
    }
  }

  function positionTooltip(el, ev) {
    // Clamp inside the viewport so the chip never spills past the right
    // edge on hovers near the end of the day-bar.
    var pad = 12;
    var rect = el.getBoundingClientRect();
    var w = rect.width || 240;
    var h = rect.height || 80;
    var x = ev.clientX + pad;
    var y = ev.clientY + pad;
    var vw = window.innerWidth || document.documentElement.clientWidth;
    var vh = window.innerHeight || document.documentElement.clientHeight;
    if (x + w + pad > vw) x = ev.clientX - w - pad;
    if (y + h + pad > vh) y = ev.clientY - h - pad;
    if (x < pad) x = pad;
    if (y < pad) y = pad;
    el.style.left = (x + window.scrollX) + 'px';
    el.style.top = (y + window.scrollY) + 'px';
  }

  function computeHhmm(barEl, clientX) {
    var rect = barEl.getBoundingClientRect();
    if (rect.width <= 0) return null;
    var rel = (clientX - rect.left) / rect.width;
    if (rel < 0) rel = 0;
    if (rel > 1) rel = 1;
    // 24h linear mapping. 1440 minutes/day total; clamp to 23:59 so we
    // never emit "2400" which the server treats as out-of-range.
    var totalMinutes = Math.floor(rel * 1440);
    if (totalMinutes >= 1440) totalMinutes = 1439;
    var hh = Math.floor(totalMinutes / 60);
    var mm = totalMinutes % 60;
    return pad2(hh) + pad2(mm);
  }

  function pad2(n) {
    return (n < 10 ? '0' : '') + String(n);
  }
})();
