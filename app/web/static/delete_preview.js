/*
 * Persona — delete-preview modal (v1.8 feature 1/3).
 *
 * Vanilla ES2020. Self-contained. No frameworks. Pairs with the v1.7
 * ``/api/screenshot/{id}/summary.json`` endpoint
 * (see app/web/routes/shot_summary.py) which returns a compact JSON
 * snapshot of the row so we can populate the modal cheaply without
 * re-fetching the whole /screenshot/{id} HTML page.
 *
 * Wiring
 * ------
 * A single document-level click listener intercepts elements carrying
 * ``data-delete-shot-id="<id>"`` (or any descendant of one). The
 * default action and propagation are cancelled so an underlying form
 * submit, an htmx ``hx-post`` form, or a plain link doesn't fire while
 * the modal is open. We then:
 *
 *   1. fetch ``/api/screenshot/{id}/summary.json``;
 *   2. render a centred card with thumbnail, app name, captured-at,
 *      OCR preview, Cancel and Delete (red) buttons;
 *   3. on Delete confirm, POST to ``/api/screenshot/{id}/delete`` —
 *      the same endpoint context_menu.js and bulk_select.js already
 *      use, which moves the row to the recycle bin;
 *   4. close the modal, then briefly flash any thumbnail still on the
 *      page green and remove it from the DOM. This visual confirms the
 *      delete succeeded without forcing a full page reload, mirroring
 *      the way bulk-select handles deletes.
 *
 * Graceful degradation
 * --------------------
 * If the summary fetch returns non-2xx (e.g. retention reaped the row
 * already, or the endpoint is missing in older deploys) we fall back
 * to a vanilla ``window.confirm`` so the operator can still complete
 * the action. Any error during the POST surfaces via ``alert`` and
 * leaves the DOM untouched. The script is a no-op until the user
 * clicks something with ``data-delete-shot-id``.
 *
 * The script is idempotent: re-loading it (e.g. htmx swap that
 * re-runs <script defer>) re-attaches the singleton listener at most
 * once thanks to a ``__personaDeletePreviewBound`` flag on window.
 */
(function () {
  'use strict';

  if (window.__personaDeletePreviewBound) {
    // The listener is registered at the document level so loading the
    // script twice would otherwise fire the handler twice for every
    // click. Bail out cleanly on the second include.
    return;
  }
  window.__personaDeletePreviewBound = true;

  // The ID of the empty <div> base.html provides for us to portal the
  // modal into. Keeping the markup outside <main> avoids weird
  // stacking-context interactions with sticky headers and Tailwind's
  // ``transform`` utility classes.
  var ROOT_ID = 'delete-preview-root';

  /** Escape a string for safe interpolation into innerHTML. */
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      }[c];
    });
  }

  /** Best-effort locale formatting for an ISO-8601 timestamp. */
  function formatCapturedAt(iso) {
    if (!iso) return '';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  /** Resolve (or lazily create) the modal root div. */
  function getRoot() {
    var root = document.getElementById(ROOT_ID);
    if (root) return root;
    // base.html should provide it, but if a page omits the include we
    // still want the modal to work — create a fallback root.
    root = document.createElement('div');
    root.id = ROOT_ID;
    document.body.appendChild(root);
    return root;
  }

  /** Tear down the modal and any listeners attached to it. */
  function closeModal() {
    var root = document.getElementById(ROOT_ID);
    if (!root) return;
    root.innerHTML = '';
    document.removeEventListener('keydown', onKeydown, true);
  }

  /** Esc closes the modal — registered only while it is open. */
  function onKeydown(ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeModal();
    }
  }

  /**
   * Find every thumbnail referencing ``shotId`` and flash it green
   * before removing it from the DOM. Covers both the /screenshot/{id}
   * single-page view (the big ``data-zoomable`` image inside a sticky
   * host) and grid pages where many ``[data-shot-card]`` cards may
   * share an id attribute.
   */
  function flashAndRemove(shotId) {
    var idStr = String(shotId);
    var targets = [];

    // Per-shot detail page wrapper.
    document.querySelectorAll('[data-sticky-host][data-shot-id="' + CSS.escape(idStr) + '"]').forEach(function (el) {
      targets.push(el);
    });
    // Grid / search-result thumbnails. The card template uses
    // [data-shot-card] with data-shot-id.
    document.querySelectorAll('[data-shot-card][data-shot-id="' + CSS.escape(idStr) + '"]').forEach(function (el) {
      targets.push(el);
    });
    // The button itself, in case nothing else matched (we still want
    // some visual confirmation).
    document.querySelectorAll('[data-delete-shot-id="' + CSS.escape(idStr) + '"]').forEach(function (el) {
      if (targets.indexOf(el) === -1) targets.push(el);
    });

    if (!targets.length) {
      // Nothing to flash — assume the operator is on a page where the
      // delete affected an off-screen row. A reload makes the new state
      // visible without us having to reason about every list view.
      setTimeout(function () { location.reload(); }, 100);
      return;
    }

    targets.forEach(function (el) {
      el.classList.add('persona-delete-preview-flash');
    });
    // Match the CSS animation duration (480ms) before removal so the
    // green flash plays to completion.
    setTimeout(function () {
      targets.forEach(function (el) {
        if (el.parentNode) el.parentNode.removeChild(el);
      });
      // On the /screenshot/{id} detail page, removing the sticky host
      // leaves a half-blank layout — kick the user back to the timeline
      // so they don't stare at a ghost page.
      var bodyShot = document.body.getAttribute('data-shot-id-active');
      if (bodyShot && String(bodyShot) === idStr) {
        location.href = '/';
      }
    }, 500);
  }

  /** POST the recycle-bin delete and wire up post-delete UI. */
  function performDelete(shotId, deleteBtn) {
    if (deleteBtn) {
      deleteBtn.disabled = true;
      deleteBtn.textContent = 'Deleting…';
    }
    fetch('/api/screenshot/' + encodeURIComponent(shotId) + '/delete', {
      method: 'POST',
      credentials: 'same-origin',
    })
      .then(function (r) {
        if (!r.ok) {
          return r.text().then(function (body) {
            throw new Error('HTTP ' + r.status + (body ? ': ' + body : ''));
          });
        }
        closeModal();
        flashAndRemove(shotId);
      })
      .catch(function (err) {
        if (deleteBtn) {
          deleteBtn.disabled = false;
          deleteBtn.textContent = 'Delete';
        }
        window.alert('Delete failed: ' + (err && err.message ? err.message : err));
      });
  }

  /**
   * Build and attach the modal markup. ``summary`` is the JSON payload
   * from /api/screenshot/{id}/summary.json. We render fields
   * defensively because v1.7 may have left some null.
   */
  function renderModal(shotId, summary) {
    var root = getRoot();
    var appName = summary && summary.app_name ? summary.app_name : 'Screenshot';
    var capturedAt = summary && summary.captured_at ? formatCapturedAt(summary.captured_at) : '';
    var ocrPreview = summary && summary.ocr_preview ? summary.ocr_preview : '';
    var thumbUrl = summary && summary.thumbnail_url ? summary.thumbnail_url : '';

    var thumbHtml = thumbUrl
      ? '<img class="persona-delete-preview-thumb" alt="" src="' + escapeHtml(thumbUrl) + '">'
      : '<div class="persona-delete-preview-thumb persona-delete-preview-thumb-empty">'
          + 'No thumbnail'
          + '</div>';

    var ocrHtml = ocrPreview
      ? '<p class="persona-delete-preview-ocr">' + escapeHtml(ocrPreview) + '</p>'
      : '';

    var metaHtml =
      '<div class="persona-delete-preview-meta">'
      +   '<span class="persona-delete-preview-app">' + escapeHtml(appName) + '</span>'
      +   (capturedAt
            ? '<span class="persona-delete-preview-time">' + escapeHtml(capturedAt) + '</span>'
            : '')
      + '</div>';

    root.innerHTML =
      '<div class="persona-delete-preview-overlay" role="presentation">'
      +   '<div class="persona-delete-preview-card" role="dialog" aria-modal="true"'
      +        ' aria-labelledby="persona-delete-preview-title">'
      +     '<h2 id="persona-delete-preview-title" class="persona-delete-preview-title">'
      +       'Move screenshot to recycle bin?'
      +     '</h2>'
      +     '<div class="persona-delete-preview-body">'
      +       thumbHtml
      +       '<div class="persona-delete-preview-text">'
      +         metaHtml
      +         ocrHtml
      +       '</div>'
      +     '</div>'
      +     '<div class="persona-delete-preview-actions">'
      +       '<button type="button" data-action="cancel"'
      +              ' class="persona-delete-preview-btn persona-delete-preview-btn-cancel">'
      +         'Cancel'
      +       '</button>'
      +       '<button type="button" data-action="confirm"'
      +              ' class="persona-delete-preview-btn persona-delete-preview-btn-delete">'
      +         'Delete'
      +       '</button>'
      +     '</div>'
      +   '</div>'
      + '</div>';

    var overlay = root.querySelector('.persona-delete-preview-overlay');
    var card = root.querySelector('.persona-delete-preview-card');
    var cancelBtn = root.querySelector('[data-action="cancel"]');
    var confirmBtn = root.querySelector('[data-action="confirm"]');

    if (overlay) {
      overlay.addEventListener('click', function (ev) {
        // Clicks on the backdrop close the modal; clicks bubbling out
        // of the card don't. Equivalent to "click outside to dismiss".
        if (ev.target === overlay) {
          closeModal();
        }
      });
    }
    if (cancelBtn) {
      cancelBtn.addEventListener('click', function (ev) {
        ev.preventDefault();
        closeModal();
      });
    }
    if (confirmBtn) {
      confirmBtn.addEventListener('click', function (ev) {
        ev.preventDefault();
        performDelete(shotId, confirmBtn);
      });
      // Auto-focus the destructive action is generally an
      // accessibility miss because Enter can wipe data. Focus Cancel
      // instead — keyboard users can Tab once to reach Delete.
    }
    if (cancelBtn) {
      try { cancelBtn.focus(); } catch (_) { /* ignore */ }
    }

    document.addEventListener('keydown', onKeydown, true);
    // Suppress card clicks bubbling to the overlay close handler.
    if (card) {
      card.addEventListener('click', function (ev) { ev.stopPropagation(); });
    }
  }

  /** Open the modal for ``shotId`` — fetches summary then renders. */
  function openModal(shotId) {
    fetch('/api/screenshot/' + encodeURIComponent(shotId) + '/summary.json', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
    })
      .then(function (r) {
        if (!r.ok) {
          // Fall back to a native confirm so the operator can still
          // proceed even if the summary endpoint is unreachable.
          if (window.confirm('Move screenshot #' + shotId + ' to the recycle bin?')) {
            performDelete(shotId, null);
          }
          return null;
        }
        return r.json();
      })
      .then(function (payload) {
        if (payload === null) return;
        renderModal(shotId, payload || {});
      })
      .catch(function () {
        if (window.confirm('Move screenshot #' + shotId + ' to the recycle bin?')) {
          performDelete(shotId, null);
        }
      });
  }

  /** Walk up from ``el`` looking for a data-delete-shot-id attribute. */
  function resolveShotId(el) {
    var node = el;
    while (node && node !== document.body) {
      if (node.nodeType === 1 && node.hasAttribute && node.hasAttribute('data-delete-shot-id')) {
        var raw = node.getAttribute('data-delete-shot-id');
        if (raw && raw.trim()) return raw.trim();
      }
      node = node.parentNode;
    }
    return null;
  }

  document.addEventListener('click', function (ev) {
    // Ignore non-primary clicks so middle-click "open in new tab" etc.
    // still work on plain anchors that happen to carry the attribute.
    if (ev.button !== 0) return;
    if (ev.defaultPrevented) return;
    var target = ev.target;
    if (!(target instanceof Element)) return;
    var shotId = resolveShotId(target);
    if (!shotId) return;
    ev.preventDefault();
    ev.stopPropagation();
    openModal(shotId);
  }, true);
})();
