/*
 * ocr_edit.js — v0.94 feature 3/3.
 *
 * Inline editor for ``screenshots.ocr_text`` on the screenshot detail
 * page. Sister to the bulk find-and-replace admin page (v0.77) and the
 * vision-replace promotion button (v0.75): both write paths already
 * snapshot the pre-edit value into ``ocr_history`` so the operator can
 * revert; this widget adds a one-off per-shot equivalent so an operator
 * looking at a single mis-recognised shot can correct it in place.
 *
 * Activation
 * ----------
 * The screenshot template renders the OCR body inside a
 *
 *   <div data-editable-ocr data-shot-id="123" contenteditable="false">
 *
 * plus three control buttons (data-ocr-edit / data-ocr-save /
 * data-ocr-cancel) that share the same ``data-shot-id``. This script
 * uses event delegation on document so the buttons keep working even
 * if HTMX swaps surrounding fragments in later.
 *
 * Edit flow
 * ---------
 * 1. Click "Edit" — the host div flips to ``contenteditable=true``,
 *    its pre-edit text is captured in ``data-original`` (so Cancel
 *    can restore it), and the Save / Cancel buttons appear while
 *    Edit hides.
 * 2. Click "Save"  — the new ``textContent`` is POSTed as form-data
 *    field ``text`` to ``/api/screenshot/{id}/ocr``. On success the
 *    div locks back to read-only and the buttons restore the idle
 *    layout. On HTTP error we surface a small inline message and
 *    leave the editor open so the operator can fix and retry.
 * 3. Click "Cancel" — the div is restored from ``data-original`` and
 *    re-locked; no request is made.
 *
 * Failure mode
 * ------------
 * The server responds with JSON ``{"ok": true, ...}`` on success or a
 * ``{"detail": "..."}`` 4xx/5xx. The fetch wrapper renders the detail
 * string into a sibling ``.ocr-edit-error`` span so the user sees the
 * real reason ("Screenshot not found", network error, etc.) instead
 * of a silent failure. ``credentials: 'same-origin'`` keeps the CSRF
 * middleware happy — same convention as the v0.92 history fetcher in
 * the surrounding template.
 */
(function () {
  'use strict';

  /** Find the editable OCR host for a given shot id. */
  function findHost(shotId) {
    return document.querySelector(
      '[data-editable-ocr][data-shot-id="' + shotId + '"]'
    );
  }

  /** Find a control button (edit/save/cancel) for a given shot id. */
  function findControl(attr, shotId) {
    return document.querySelector(
      '[' + attr + '][data-shot-id="' + shotId + '"]'
    );
  }

  /** Find or lazily create the inline error span next to a host. */
  function ensureErrorSlot(host) {
    var slot = host.parentNode
      ? host.parentNode.querySelector('.ocr-edit-error')
      : null;
    if (slot) return slot;
    slot = document.createElement('span');
    slot.className = 'ocr-edit-error text-rose-400 text-xs ml-2';
    slot.setAttribute('role', 'alert');
    if (host.parentNode) host.parentNode.appendChild(slot);
    return slot;
  }

  function showError(host, message) {
    var slot = ensureErrorSlot(host);
    slot.textContent = message || '';
  }

  function clearError(host) {
    var slot = host.parentNode
      ? host.parentNode.querySelector('.ocr-edit-error')
      : null;
    if (slot) slot.textContent = '';
  }

  /** Show/hide the per-state control buttons for a shot. */
  function setEditingUi(shotId, editing) {
    var edit = findControl('data-ocr-edit', shotId);
    var save = findControl('data-ocr-save', shotId);
    var cancel = findControl('data-ocr-cancel', shotId);
    if (edit) edit.hidden = editing;
    if (save) save.hidden = !editing;
    if (cancel) cancel.hidden = !editing;
  }

  function beginEdit(shotId) {
    var host = findHost(shotId);
    if (!host) return;
    clearError(host);
    // Capture the rendered text *before* the operator types so Cancel
    // can faithfully restore even if the template emitted markup
    // (e.g. linkified URLs as <a> tags). ``textContent`` flattens HTML
    // to plaintext, which matches what the server stores anyway.
    host.setAttribute('data-original', host.textContent || '');
    host.setAttribute('contenteditable', 'true');
    host.classList.add('ocr-editing');
    setEditingUi(shotId, true);
    host.focus();
  }

  function cancelEdit(shotId) {
    var host = findHost(shotId);
    if (!host) return;
    var original = host.getAttribute('data-original');
    if (original !== null) host.textContent = original;
    host.setAttribute('contenteditable', 'false');
    host.classList.remove('ocr-editing');
    setEditingUi(shotId, false);
    clearError(host);
  }

  async function saveEdit(shotId) {
    var host = findHost(shotId);
    if (!host) return;
    clearError(host);
    var newText = host.textContent || '';
    var saveBtn = findControl('data-ocr-save', shotId);
    if (saveBtn) saveBtn.disabled = true;
    try {
      var fd = new FormData();
      fd.append('text', newText);
      var r = await fetch('/api/screenshot/' + shotId + '/ocr', {
        method: 'POST',
        body: fd,
        credentials: 'same-origin',
      });
      if (!r.ok) {
        var detail = 'HTTP ' + r.status;
        try {
          var j = await r.json();
          if (j && j.detail) detail = j.detail;
        } catch (_err) {
          // body wasn't JSON — keep the status-code fallback.
        }
        showError(host, 'Save failed: ' + detail);
        return;
      }
      // Lock the editor; the server is the source of truth for the
      // FTS-indexed value, so we accept the round-trip text as final.
      host.setAttribute('contenteditable', 'false');
      host.classList.remove('ocr-editing');
      host.removeAttribute('data-original');
      setEditingUi(shotId, false);
    } catch (_err) {
      showError(host, 'Save failed: network error');
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    if (!(target instanceof Element)) return;

    var editBtn = target.closest('[data-ocr-edit]');
    if (editBtn) {
      event.preventDefault();
      beginEdit(editBtn.getAttribute('data-shot-id') || '');
      return;
    }
    var saveBtn = target.closest('[data-ocr-save]');
    if (saveBtn) {
      event.preventDefault();
      saveEdit(saveBtn.getAttribute('data-shot-id') || '');
      return;
    }
    var cancelBtn = target.closest('[data-ocr-cancel]');
    if (cancelBtn) {
      event.preventDefault();
      cancelEdit(cancelBtn.getAttribute('data-shot-id') || '');
      return;
    }
  });
})();
