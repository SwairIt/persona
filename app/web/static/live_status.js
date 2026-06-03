/* live_status.js — v0.40
 *
 * Header status-pill driver. Replaces the legacy 5-second poll loop
 * with a single Server-Sent Events connection at /events.
 *
 * Server contract (see app/web/routes/live_sse.py):
 *   - "status"    -> {capture_running, ocr_pending, last_capture_at,
 *                     today_shots, total_shots}
 *   - "heartbeat" -> {worker_name, last_run_at}
 *
 * DOM contract:
 *   #status-pill              — root element, gets data-state="capturing|paused"
 *   #status-pill-shots        — text count of today's screenshots
 *   #status-pill-heartbeat-dot — pulses green for ~600ms when a worker beats
 *   #total-shots-count        — v0.87 dashboard live-count widget; updated
 *                                from payload.total_shots when present
 *
 * The script is a no-op in browsers without EventSource (very old IE /
 * locked-down embeds). It reconnects with exponential backoff after an
 * abnormal close, capped at 30s.
 */
(function () {
  "use strict";

  if (typeof window === "undefined" || typeof window.EventSource !== "function") {
    return;
  }

  var ENDPOINT = "/events";
  var MIN_BACKOFF_MS = 1000;
  var MAX_BACKOFF_MS = 30000;
  var HEARTBEAT_FLASH_MS = 600;

  var source = null;
  var backoff = MIN_BACKOFF_MS;
  var reconnectTimer = null;
  var heartbeatTimer = null;

  function $(id) {
    return document.getElementById(id);
  }

  function applyStatus(payload) {
    if (!payload || typeof payload !== "object") {
      return;
    }
    var pill = $("status-pill");
    if (pill) {
      pill.setAttribute(
        "data-state",
        payload.capture_running ? "capturing" : "paused"
      );
      if (payload.last_capture_at) {
        pill.setAttribute("data-last-capture-at", payload.last_capture_at);
      }
      if (typeof payload.ocr_pending === "number") {
        pill.setAttribute("data-ocr-pending", String(payload.ocr_pending));
      }
    }
    var shots = $("status-pill-shots");
    if (shots && typeof payload.today_shots === "number") {
      shots.textContent = String(payload.today_shots);
    }
    // v0.87 — dashboard live-count widget. Element is optional; only the
    // dashboard renders it, but the SSE payload is shared by every page.
    var total = $("total-shots-count");
    if (total && typeof payload.total_shots === "number") {
      total.textContent = String(payload.total_shots);
    }
  }

  function flashHeartbeat(payload) {
    var dot = $("status-pill-heartbeat-dot");
    if (!dot) {
      return;
    }
    dot.classList.add("is-beating");
    if (payload && payload.worker_name) {
      dot.setAttribute("data-worker", String(payload.worker_name));
    }
    if (heartbeatTimer) {
      window.clearTimeout(heartbeatTimer);
    }
    heartbeatTimer = window.setTimeout(function () {
      dot.classList.remove("is-beating");
      heartbeatTimer = null;
    }, HEARTBEAT_FLASH_MS);
  }

  function handleEvent(raw) {
    var parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (e) {
      return;
    }
    if (!parsed || typeof parsed.type !== "string") {
      return;
    }
    if (parsed.type === "status") {
      applyStatus(parsed.payload);
    } else if (parsed.type === "heartbeat") {
      flashHeartbeat(parsed.payload);
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) {
      return;
    }
    var delay = backoff;
    backoff = Math.min(MAX_BACKOFF_MS, backoff * 2);
    reconnectTimer = window.setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    try {
      source = new window.EventSource(ENDPOINT);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    source.addEventListener("open", function () {
      backoff = MIN_BACKOFF_MS;
    });

    source.addEventListener("message", function (ev) {
      handleEvent(ev.data);
    });

    source.addEventListener("error", function () {
      // EventSource auto-reconnects on transient errors. We close +
      // re-open ourselves only after a hard CLOSED state so we keep
      // explicit control over the backoff curve.
      if (source && source.readyState === window.EventSource.CLOSED) {
        try {
          source.close();
        } catch (e) {
          /* ignore */
        }
        source = null;
        scheduleReconnect();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", connect, { once: true });
  } else {
    connect();
  }

  window.addEventListener("beforeunload", function () {
    if (source) {
      try {
        source.close();
      } catch (e) {
        /* ignore */
      }
    }
  });
})();
