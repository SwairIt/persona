// Browser push-notification opt-in (v0.66).
//
// Exposes one global, `personaEnableNotif()`, wired to a button click in
// settings.html. We deliberately do NOT call Notification.requestPermission
// at module load — Chrome and Firefox both swallow permission requests that
// aren't tied to a real user gesture, so the prompt has to live behind the
// click handler.
//
// Once permission is granted, the page POSTs /api/push-notif/enable to
// flip the kv_settings flag and starts a 5-minute setInterval polling
// /api/push-notif/pending. Each row returned is materialised as a fresh
// `new Notification(title, { body })`. The interval is intentionally
// stored in a module-level guard so re-clicking the button on the same
// page load doesn't stack timers.

(() => {
  'use strict';

  const POLL_INTERVAL_MS = 5 * 60 * 1000;
  const PENDING_URL = '/api/push-notif/pending';
  const ENABLE_URL = '/api/push-notif/enable';

  /** @type {number | null} */
  let pollTimer = null;

  /**
   * Fetch the pending-notifications queue and surface each row.
   * Network errors are swallowed to a console.warn — a flaky poll
   * shouldn't bubble up to the user with a red toast every 5 minutes.
   */
  async function pollPending() {
    try {
      const res = await fetch(PENDING_URL, {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!res.ok) {
        console.warn('push_notif: pending poll returned', res.status);
        return;
      }
      const data = await res.json();
      const items = Array.isArray(data?.notifications) ? data.notifications : [];
      for (const item of items) {
        if (typeof item?.title !== 'string') continue;
        const body = typeof item?.body === 'string' ? item.body : '';
        try {
          new Notification(item.title, { body });
        } catch (err) {
          console.warn('push_notif: Notification ctor threw', err);
        }
      }
    } catch (err) {
      console.warn('push_notif: pending poll failed', err);
    }
  }

  /**
   * Start the 5-minute poll loop, idempotently. Fires one immediate
   * poll so a freshly-enabled tab doesn't have to wait 5 minutes to
   * see any backlog the server has waiting.
   */
  function startPolling() {
    if (pollTimer !== null) return;
    pollTimer = window.setInterval(pollPending, POLL_INTERVAL_MS);
    void pollPending();
  }

  /**
   * Public entry point for the Settings page button.
   * Idempotent: a second click after a granted prompt just re-POSTs
   * /enable and re-arms the timer (which is a no-op).
   */
  window.personaEnableNotif = async function personaEnableNotif() {
    if (typeof window.Notification === 'undefined') {
      window.alert('This browser does not support desktop notifications.');
      return;
    }

    let permission = Notification.permission;
    if (permission === 'default') {
      try {
        permission = await Notification.requestPermission();
      } catch (err) {
        console.warn('push_notif: requestPermission threw', err);
        return;
      }
    }

    if (permission !== 'granted') {
      console.info('push_notif: permission not granted, state =', permission);
      return;
    }

    try {
      const res = await fetch(ENABLE_URL, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!res.ok) {
        console.warn('push_notif: enable POST returned', res.status);
        return;
      }
    } catch (err) {
      console.warn('push_notif: enable POST failed', err);
      return;
    }

    startPolling();
  };
})();
