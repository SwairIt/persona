/* drag_drop_import.js — v0.94 feature 2/3
 *
 * Screenshot drag-and-drop import. The user grabs an image file from
 * Explorer / Finder / a browser tab and drops it anywhere on the
 * /timeline page; the file is POSTed to /api/import-screenshot as
 * multipart form data and the page reloads so the new shot appears.
 *
 * The script only takes over the drop event when the dragged payload
 * actually contains a file. Tag chips (data-draggable-tag) and any
 * other in-page drag use ``application/x-persona-*`` MIME types, so
 * the file-only guard below lets those continue to work untouched.
 *
 * Server contract:
 *   POST /api/import-screenshot
 *     multipart/form-data ; file=<the dropped File>
 *     - 201 -> { ok: true, screenshot_id, captured_at, width, height,
 *                format, filename }
 *     - 4xx -> { detail: "<reason>" }
 *
 * UX:
 *   * dragover anywhere on the document shows a faint full-page
 *     overlay with "Drop to import" so the user knows the drop is
 *     valid.
 *   * On a successful drop we flash the overlay green for ~600ms,
 *     then reload the page so the new screenshot card lands at the
 *     top of the timeline. On failure we flash red and surface the
 *     server's ``detail`` string in the overlay text.
 *   * Only the first file in a multi-file drop is imported — a single
 *     reload per drop is what the timeline expects. A "n files
 *     queued; importing 1" hint covers the edge case.
 *
 * The script is loaded on every page (via base.html) but the import
 * only fires while the user is on / or /timeline so we do not silently
 * import a file the user dropped on, say, the settings page. Other
 * pages still get the full-page dragover guard so a stray drop does
 * not navigate the browser away from the SPA.
 */
(function () {
  "use strict";

  if (typeof document === "undefined" || typeof window === "undefined") {
    return;
  }

  var ENDPOINT = "/api/import-screenshot";
  // 10 MiB — must match _MAX_UPLOAD_BYTES in
  // app/web/routes/import_screenshot.py. The server is the source of
  // truth; the client check just spares the user a wasted upload.
  var MAX_BYTES = 10 * 1024 * 1024;
  var ALLOWED_TYPES = ["image/png", "image/jpeg", "image/jpg"];
  // Bare-extension fallback for clients that drop a File without a
  // populated ``type`` (Windows Explorer occasionally does this for
  // files copied from a network share).
  var ALLOWED_EXTENSIONS = [".png", ".jpg", ".jpeg"];

  function isTimelineRoute() {
    var p = window.location.pathname;
    return p === "/" || p === "/timeline" || p.indexOf("/timeline/") === 0;
  }

  function dragHasFiles(event) {
    if (!event || !event.dataTransfer) {
      return false;
    }
    var types = event.dataTransfer.types;
    if (!types) {
      return false;
    }
    // ``types`` is a DOMStringList in legacy browsers and an Array
    // in modern ones — both expose ``length`` + index access.
    for (var i = 0; i < types.length; i += 1) {
      if (types[i] === "Files") {
        return true;
      }
    }
    return false;
  }

  function looksLikeImage(file) {
    if (!file) {
      return false;
    }
    if (file.type && ALLOWED_TYPES.indexOf(file.type.toLowerCase()) >= 0) {
      return true;
    }
    var name = (file.name || "").toLowerCase();
    for (var i = 0; i < ALLOWED_EXTENSIONS.length; i += 1) {
      if (name.endsWith(ALLOWED_EXTENSIONS[i])) {
        return true;
      }
    }
    return false;
  }

  // --- Overlay ----------------------------------------------------------

  var overlay = null;
  var overlayText = null;
  var hideTimer = null;

  function ensureOverlay() {
    if (overlay) {
      return overlay;
    }
    overlay = document.createElement("div");
    overlay.id = "persona-drop-overlay";
    overlay.setAttribute("aria-hidden", "true");
    overlay.style.cssText = [
      "position:fixed",
      "inset:0",
      "display:none",
      "align-items:center",
      "justify-content:center",
      "background:rgba(10,10,12,0.55)",
      "color:#f4f4f5",
      "font:600 18px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif",
      "z-index:9999",
      "pointer-events:none",
      "backdrop-filter:blur(4px)",
      "-webkit-backdrop-filter:blur(4px)",
      "transition:background-color 180ms ease",
    ].join(";");

    var box = document.createElement("div");
    box.style.cssText = [
      "padding:24px 36px",
      "border-radius:12px",
      "border:2px dashed rgba(167,139,250,0.8)",
      "background:rgba(26,26,31,0.85)",
      "text-align:center",
      "max-width:80%",
    ].join(";");

    overlayText = document.createElement("div");
    overlayText.textContent = "Drop to import screenshot";
    box.appendChild(overlayText);

    var hint = document.createElement("div");
    hint.textContent = "PNG or JPEG, up to 10 MB";
    hint.style.cssText = "margin-top:6px;font-weight:400;font-size:13px;color:#a1a1aa;";
    box.appendChild(hint);

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    return overlay;
  }

  function showOverlay(message) {
    var el = ensureOverlay();
    if (overlayText && typeof message === "string") {
      overlayText.textContent = message;
    }
    el.style.display = "flex";
    if (hideTimer) {
      window.clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  function flashOverlay(message, colour, holdMs) {
    var el = ensureOverlay();
    if (overlayText) {
      overlayText.textContent = message;
    }
    el.style.background = colour;
    el.style.display = "flex";
    if (hideTimer) {
      window.clearTimeout(hideTimer);
    }
    hideTimer = window.setTimeout(function () {
      hideOverlay();
    }, holdMs);
  }

  function hideOverlay() {
    if (!overlay) {
      return;
    }
    overlay.style.display = "none";
    overlay.style.background = "rgba(10,10,12,0.55)";
    if (overlayText) {
      overlayText.textContent = "Drop to import screenshot";
    }
    if (hideTimer) {
      window.clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  // --- Upload -----------------------------------------------------------

  function uploadFile(file) {
    var form = new FormData();
    form.append("file", file, file.name || "manual.png");
    showOverlay("Uploading " + (file.name || "screenshot") + "…");
    return fetch(ENDPOINT, {
      method: "POST",
      body: form,
      credentials: "same-origin",
    })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            return { response: response, body: body };
          });
      })
      .then(function (result) {
        if (!result.response.ok) {
          var detail = "Import failed (HTTP " + result.response.status + ")";
          if (result.body && typeof result.body.detail === "string") {
            detail = result.body.detail;
          }
          flashOverlay("Error: " + detail, "rgba(190,18,60,0.65)", 2400);
          return;
        }
        flashOverlay("Imported — reloading…", "rgba(5,150,105,0.65)", 600);
        window.setTimeout(function () {
          window.location.reload();
        }, 650);
      })
      .catch(function (err) {
        flashOverlay(
          "Error: " + (err && err.message ? err.message : "network failure"),
          "rgba(190,18,60,0.65)",
          2400
        );
      });
  }

  // --- Event wiring -----------------------------------------------------

  // Cross-platform drag tracking: dragenter / dragleave on individual
  // elements fires repeatedly as the pointer crosses child boundaries,
  // so we keep a depth counter to only hide the overlay when the
  // pointer truly leaves the window.
  var enterCount = 0;

  document.addEventListener(
    "dragenter",
    function (event) {
      if (!dragHasFiles(event)) {
        return;
      }
      event.preventDefault();
      enterCount += 1;
      showOverlay();
    },
    false
  );

  document.addEventListener(
    "dragover",
    function (event) {
      if (!dragHasFiles(event)) {
        return;
      }
      // ``preventDefault`` is required for the drop event to fire on
      // a non-form-input target; without it the browser navigates to
      // the dropped file and the user loses their place.
      event.preventDefault();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
    },
    false
  );

  document.addEventListener(
    "dragleave",
    function (event) {
      if (!dragHasFiles(event)) {
        return;
      }
      enterCount = Math.max(0, enterCount - 1);
      if (enterCount === 0) {
        hideOverlay();
      }
    },
    false
  );

  document.addEventListener(
    "drop",
    function (event) {
      if (!dragHasFiles(event)) {
        return;
      }
      event.preventDefault();
      enterCount = 0;

      if (!isTimelineRoute()) {
        // We still want to swallow the drop so the browser doesn't
        // navigate to the file URL, but we tell the user explicitly
        // that the drop only works on the timeline page.
        flashOverlay(
          "Drop on /timeline to import",
          "rgba(202,138,4,0.65)",
          1800
        );
        return;
      }

      var files =
        event.dataTransfer && event.dataTransfer.files
          ? event.dataTransfer.files
          : null;
      if (!files || files.length === 0) {
        hideOverlay();
        return;
      }
      var first = files[0];
      if (!looksLikeImage(first)) {
        flashOverlay(
          "Not a PNG / JPEG: " + (first.name || "file"),
          "rgba(190,18,60,0.65)",
          2200
        );
        return;
      }
      if (typeof first.size === "number" && first.size > MAX_BYTES) {
        flashOverlay(
          "File too large (max 10 MB)",
          "rgba(190,18,60,0.65)",
          2200
        );
        return;
      }
      if (files.length > 1) {
        // Surface the truncation so the user is not confused why
        // only one of their drops landed.
        showOverlay(
          files.length + " files dropped — importing 1 (" + first.name + ")"
        );
      }
      uploadFile(first);
    },
    false
  );
})();
