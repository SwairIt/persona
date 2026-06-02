/* drag_to_tag.js — v0.41
 *
 * Drag-to-tag: drag a tag chip from the sidebar onto a screenshot
 * thumbnail to apply that tag. Pure HTML5 drag-and-drop, no framework.
 *
 * DOM contract:
 *   [data-draggable-tag="<name>"]      — sources (tag chips). MUST also
 *                                        carry draggable="true" so the
 *                                        browser starts a drag session.
 *   [data-drop-target-shot][data-shot-id="<id>"]
 *                                      — drop zones (thumbnail wrappers).
 *
 * Server contract:
 *   POST /api/screenshot/{id}/tags  (form: tag=<name>)
 *     - 2xx -> success
 *     - 4xx -> validation error (empty name / missing shot)
 *
 * On a successful drop we briefly green-flash the thumbnail border, and
 * fire a "persona:tag-applied" custom event so any HTMX listener
 * (hx-trigger="persona:tag-applied from:body") can refresh its tag
 * listing without a full page reload. On 4xx/5xx we red-flash instead.
 *
 * The script is idempotent — it re-runs on htmx swaps so newly-rendered
 * chips and thumbnails pick up listeners automatically.
 */
(function () {
  "use strict";

  if (typeof document === "undefined") {
    return;
  }

  var DRAG_MIME = "application/x-persona-tag";
  var FLASH_MS = 700;
  var BOUND_ATTR = "data-drag-bound";

  function bindSources(root) {
    var nodes = (root || document).querySelectorAll(
      "[data-draggable-tag]:not([" + BOUND_ATTR + "])"
    );
    nodes.forEach(function (el) {
      el.setAttribute(BOUND_ATTR, "1");
      // Ensure the element is actually draggable even if the template
      // forgot the attribute.
      if (!el.hasAttribute("draggable")) {
        el.setAttribute("draggable", "true");
      }
      el.addEventListener("dragstart", onDragStart);
      el.addEventListener("dragend", onDragEnd);
    });
  }

  function bindTargets(root) {
    var nodes = (root || document).querySelectorAll(
      "[data-drop-target-shot]:not([" + BOUND_ATTR + "])"
    );
    nodes.forEach(function (el) {
      el.setAttribute(BOUND_ATTR, "1");
      el.addEventListener("dragenter", onDragEnter);
      el.addEventListener("dragover", onDragOver);
      el.addEventListener("dragleave", onDragLeave);
      el.addEventListener("drop", onDrop);
    });
  }

  function onDragStart(ev) {
    var name = ev.currentTarget.getAttribute("data-draggable-tag") || "";
    if (!name) {
      return;
    }
    try {
      ev.dataTransfer.setData(DRAG_MIME, name);
      // Fallback for browsers that strip custom MIME types.
      ev.dataTransfer.setData("text/plain", name);
      ev.dataTransfer.effectAllowed = "copy";
    } catch (e) {
      /* setData may throw in locked-down embeds */
    }
    ev.currentTarget.classList.add("opacity-60");
  }

  function onDragEnd(ev) {
    ev.currentTarget.classList.remove("opacity-60");
  }

  function onDragEnter(ev) {
    if (!hasTagPayload(ev)) {
      return;
    }
    ev.preventDefault();
    ev.currentTarget.classList.add("ring-2", "ring-accent-400");
  }

  function onDragOver(ev) {
    if (!hasTagPayload(ev)) {
      return;
    }
    ev.preventDefault();
    if (ev.dataTransfer) {
      ev.dataTransfer.dropEffect = "copy";
    }
  }

  function onDragLeave(ev) {
    // Only clear when leaving the wrapper itself, not when crossing into
    // an inner child element.
    if (ev.currentTarget.contains(ev.relatedTarget)) {
      return;
    }
    ev.currentTarget.classList.remove("ring-2", "ring-accent-400");
  }

  function onDrop(ev) {
    ev.preventDefault();
    var target = ev.currentTarget;
    target.classList.remove("ring-2", "ring-accent-400");

    var tagName = "";
    if (ev.dataTransfer) {
      tagName = ev.dataTransfer.getData(DRAG_MIME)
        || ev.dataTransfer.getData("text/plain")
        || "";
    }
    tagName = tagName.trim();
    var shotId = target.getAttribute("data-shot-id") || "";
    if (!tagName || !shotId) {
      flash(target, false);
      return;
    }

    applyTag(shotId, tagName).then(function (ok) {
      flash(target, ok);
      if (ok) {
        document.body.dispatchEvent(
          new CustomEvent("persona:tag-applied", {
            detail: { shotId: shotId, tag: tagName },
            bubbles: true,
          })
        );
      }
    });
  }

  function applyTag(shotId, tagName) {
    var fd = new FormData();
    fd.append("tag", tagName);
    return fetch("/api/screenshot/" + encodeURIComponent(shotId) + "/tags", {
      method: "POST",
      body: fd,
      credentials: "same-origin",
    })
      .then(function (r) {
        return r.ok;
      })
      .catch(function () {
        return false;
      });
  }

  function hasTagPayload(ev) {
    if (!ev.dataTransfer || !ev.dataTransfer.types) {
      return false;
    }
    var types = ev.dataTransfer.types;
    // DataTransferItemList isn't a real Array in older browsers.
    for (var i = 0; i < types.length; i++) {
      if (types[i] === DRAG_MIME || types[i] === "text/plain") {
        return true;
      }
    }
    return false;
  }

  function flash(el, ok) {
    var cls = ok ? "ring-2 ring-emerald-400" : "ring-2 ring-rose-500";
    var parts = cls.split(" ");
    parts.forEach(function (c) {
      el.classList.add(c);
    });
    window.setTimeout(function () {
      parts.forEach(function (c) {
        el.classList.remove(c);
      });
    }, FLASH_MS);
  }

  function bindAll(root) {
    bindSources(root);
    bindTargets(root);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      bindAll(document);
    });
  } else {
    bindAll(document);
  }

  // Re-bind after HTMX swaps so server-rendered chips/thumbnails pick up
  // listeners without a full reload.
  document.body && document.body.addEventListener("htmx:afterSwap", function (ev) {
    bindAll(ev.target || document);
  });
})();
