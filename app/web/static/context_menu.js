/* context_menu.js — v0.43
 *
 * Right-click context menu for screenshot thumbnails.
 *
 * DOM contract
 *   Any element carrying both [data-shot-id] and a `contextmenu` target
 *   semantics (typically the thumbnail wrapper, e.g. the <a> rendered by
 *   templates/_screenshot_card.html) becomes a right-clickable surface.
 *
 *   We attach ONE delegated listener on document so the menu picks up
 *   server-rendered thumbnails (timeline, search results, favourites,
 *   collections, ...) and any future htmx swaps without per-element
 *   binding.
 *
 * Server contract
 *   Pin            POST /api/screenshots/{id}/pin
 *   Unpin          POST /api/screenshots/{id}/unpin
 *   Favourite      POST /api/screenshot/{id}/favourite        (v0.29)
 *   Open           navigate to /screenshot/{id}
 *   Add tag        POST /api/screenshot/{id}/tags             (v0.41,
 *                       form field: tag=<name>)
 *   Add to coll.   GET  /api/collections then
 *                  POST /api/screenshot/{id}/tags             (tag of
 *                       the chosen collection — that's how auto-
 *                       collections compute membership in v0.23)
 *   Delete         POST /api/screenshot/{id}/delete           (recycle
 *                       bin, v0.40)
 *
 * Each action flashes the source thumbnail green on success or red on
 * failure (see .persona-ctx-flash--ok / --err in context_menu.css).
 *
 * Dismisses on:
 *   - left/middle click anywhere outside the menu
 *   - Escape
 *   - another contextmenu invocation (the old menu is replaced)
 *   - scroll / resize / blur (menus that drift away from the cursor
 *     are worse than no menu at all)
 *
 * ES2020, vanilla, no dependencies.
 */
(function () {
  "use strict";

  if (typeof document === "undefined") {
    return;
  }

  const MENU_ID = "persona-ctx-menu";
  const FLASH_MS = 700;
  const VIEWPORT_PAD = 8;

  /** Lazily build the single shared menu element. */
  function ensureMenu() {
    let menu = document.getElementById(MENU_ID);
    if (menu) {
      return menu;
    }
    menu = document.createElement("ul");
    menu.id = MENU_ID;
    menu.className = "persona-ctx-menu";
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    // Clicks inside the menu are handled by item-level listeners; stop
    // the document-level "click outside" handler from firing on them.
    menu.addEventListener("mousedown", function (ev) {
      ev.stopPropagation();
    });
    document.body.appendChild(menu);
    return menu;
  }

  function hideMenu() {
    const menu = document.getElementById(MENU_ID);
    if (!menu || menu.hidden) {
      return;
    }
    menu.hidden = true;
    menu.innerHTML = "";
    menu.dataset.shotId = "";
  }

  /** Place the menu so it stays inside the viewport. */
  function positionMenu(menu, x, y) {
    // Render off-screen first to measure.
    menu.style.left = "-9999px";
    menu.style.top = "-9999px";
    menu.hidden = false;
    const rect = menu.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let nx = x;
    let ny = y;
    if (nx + rect.width + VIEWPORT_PAD > vw) {
      nx = Math.max(VIEWPORT_PAD, vw - rect.width - VIEWPORT_PAD);
    }
    if (ny + rect.height + VIEWPORT_PAD > vh) {
      ny = Math.max(VIEWPORT_PAD, vh - rect.height - VIEWPORT_PAD);
    }
    menu.style.left = nx + "px";
    menu.style.top = ny + "px";
  }

  /** Build the <li> for one action. */
  function makeItem(label, icon, handler, opts) {
    const li = document.createElement("li");
    li.className = "persona-ctx-menu__item";
    if (opts && opts.danger) {
      li.classList.add("persona-ctx-menu__item--danger");
    }
    li.setAttribute("role", "menuitem");
    li.tabIndex = 0;
    const iconEl = document.createElement("span");
    iconEl.className = "persona-ctx-menu__icon";
    iconEl.textContent = icon || "";
    iconEl.setAttribute("aria-hidden", "true");
    const text = document.createElement("span");
    text.textContent = label;
    li.appendChild(iconEl);
    li.appendChild(text);
    li.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      hideMenu();
      Promise.resolve()
        .then(handler)
        .catch(function (err) {
          // Surfacing failures via the flash is enough for users; the
          // console trace remains useful for the developer.
          // eslint-disable-next-line no-console
          console.warn("persona context menu action failed:", err);
        });
    });
    li.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        li.click();
      }
    });
    return li;
  }

  function makeSeparator() {
    const li = document.createElement("li");
    li.className = "persona-ctx-menu__sep";
    li.setAttribute("role", "separator");
    return li;
  }

  /** Apply a short coloured outline to the originating thumbnail. */
  function flash(target, ok) {
    if (!target || !target.classList) {
      return;
    }
    const cls = ok ? "persona-ctx-flash--ok" : "persona-ctx-flash--err";
    target.classList.add("persona-ctx-flash", cls);
    window.setTimeout(function () {
      target.classList.remove(cls);
      // Drop the wrapper class only when no other flash variant is left.
      if (
        !target.classList.contains("persona-ctx-flash--ok") &&
        !target.classList.contains("persona-ctx-flash--err")
      ) {
        target.classList.remove("persona-ctx-flash");
      }
    }, FLASH_MS);
  }

  /** POST a form/empty body and resolve to a boolean ok flag. */
  function postForm(url, formData) {
    const init = { method: "POST", credentials: "same-origin" };
    if (formData) {
      init.body = formData;
    }
    return fetch(url, init).then(function (r) {
      return r.ok;
    });
  }

  function pinShot(shotId) {
    return postForm("/api/screenshots/" + encodeURIComponent(shotId) + "/pin");
  }

  function unpinShot(shotId) {
    return postForm("/api/screenshots/" + encodeURIComponent(shotId) + "/unpin");
  }

  function toggleFavourite(shotId) {
    return postForm("/api/screenshot/" + encodeURIComponent(shotId) + "/favourite");
  }

  function addTag(shotId, name) {
    const fd = new FormData();
    fd.append("tag", name);
    return postForm("/api/screenshot/" + encodeURIComponent(shotId) + "/tags", fd);
  }

  function deleteShot(shotId) {
    return postForm("/api/screenshot/" + encodeURIComponent(shotId) + "/delete");
  }

  function fetchCollections() {
    return fetch("/api/collections", { credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) {
          return { rules: [] };
        }
        return r.json();
      })
      .catch(function () {
        return { rules: [] };
      });
  }

  /** Whether the thumbnail's template marks it as already pinned. */
  function isPinned(target) {
    // The card template emits a pin glyph inside [data-shot-card] when
    // shot.tier == 'pinned'. We tolerate either an explicit attribute
    // (data-pinned) or the visible glyph.
    if (!target) {
      return false;
    }
    if (target.dataset && target.dataset.pinned === "1") {
      return true;
    }
    const pinIcon = target.querySelector('[title="Pinned"]');
    return !!pinIcon;
  }

  /**
   * Whether the thumbnail's template marks it as locked (v0.70).
   *
   * Locked shots opt out of every soft-delete path: the bulk-delete
   * job filters them server-side, and here in the context menu we
   * suppress the Delete entry entirely so the user is never even
   * offered the destructive action. The server still rejects a
   * forged POST via `ShotLocked` — this check is only a courtesy
   * to keep the UI consistent with the data layer.
   */
  function isLocked(target) {
    if (!target || !target.dataset) {
      return false;
    }
    return target.dataset.locked === "1";
  }

  /** Build and show the menu for one specific shot. */
  function openMenuFor(target, shotId, x, y) {
    const menu = ensureMenu();
    menu.innerHTML = "";
    menu.dataset.shotId = String(shotId);

    const pinned = isPinned(target);

    menu.appendChild(
      makeItem("Open", "↗", function () {
        window.location.assign("/screenshot/" + encodeURIComponent(shotId));
      })
    );

    menu.appendChild(makeSeparator());

    menu.appendChild(
      makeItem(pinned ? "Unpin" : "Pin", "📌", function () {
        const action = pinned ? unpinShot(shotId) : pinShot(shotId);
        return action
          .then(function (ok) {
            flash(target, ok);
          })
          .catch(function () {
            flash(target, false);
          });
      })
    );

    menu.appendChild(
      makeItem("Favourite", "★", function () {
        return toggleFavourite(shotId)
          .then(function (ok) {
            flash(target, ok);
          })
          .catch(function () {
            flash(target, false);
          });
      })
    );

    menu.appendChild(makeSeparator());

    menu.appendChild(
      makeItem("Add tag…", "#", function () {
        const raw = window.prompt("Tag name");
        if (raw === null) {
          return undefined;
        }
        const name = raw.trim();
        if (!name) {
          flash(target, false);
          return undefined;
        }
        return addTag(shotId, name)
          .then(function (ok) {
            flash(target, ok);
            if (ok) {
              document.body.dispatchEvent(
                new CustomEvent("persona:tag-applied", {
                  detail: { shotId: String(shotId), tag: name },
                  bubbles: true,
                })
              );
            }
          })
          .catch(function () {
            flash(target, false);
          });
      })
    );

    menu.appendChild(
      makeItem("Add to collection…", "▦", function () {
        return fetchCollections().then(function (data) {
          const rules = (data && data.rules) || [];
          if (rules.length === 0) {
            window.alert(
              "No auto-collections yet. Create one at /collections first."
            );
            return undefined;
          }
          const lines = rules.map(function (r, idx) {
            return idx + 1 + ". " + r.title + "  [" + r.slug + " → #" + r.tag + "]";
          });
          const ans = window.prompt(
            "Add to which collection?\n\n" + lines.join("\n") + "\n\nEnter a number:"
          );
          if (ans === null) {
            return undefined;
          }
          const idx = parseInt(ans.trim(), 10) - 1;
          if (!Number.isInteger(idx) || idx < 0 || idx >= rules.length) {
            flash(target, false);
            return undefined;
          }
          const rule = rules[idx];
          // Auto-collection membership = carrying the rule's tag (v0.23).
          return addTag(shotId, rule.tag)
            .then(function (ok) {
              flash(target, ok);
              if (ok) {
                document.body.dispatchEvent(
                  new CustomEvent("persona:tag-applied", {
                    detail: {
                      shotId: String(shotId),
                      tag: rule.tag,
                      collection: rule.slug,
                    },
                    bubbles: true,
                  })
                );
              }
            })
            .catch(function () {
              flash(target, false);
            });
        });
      })
    );

    // v0.70 — locked shots never offer a Delete entry. The server
    // also rejects the underlying POST (recycle.ShotLocked), but the
    // menu suppression keeps the UI honest: a destructive action the
    // server will refuse should never appear in the first place.
    if (!isLocked(target)) {
      menu.appendChild(makeSeparator());

      menu.appendChild(
        makeItem(
          "Delete",
          "🗑",
          function () {
            const ok = window.confirm(
              "Move screenshot #" + shotId + " to the recycle bin?"
            );
            if (!ok) {
              return undefined;
            }
            return deleteShot(shotId)
              .then(function (success) {
                flash(target, success);
                if (success) {
                  // Best-effort: fade the thumbnail out of the current
                  // grid so the user can see the row leave without a
                  // page reload.
                  target.style.transition = "opacity 240ms ease";
                  target.style.opacity = "0.25";
                  target.style.pointerEvents = "none";
                }
              })
              .catch(function () {
                flash(target, false);
              });
          },
          { danger: true }
        )
      );
    }

    positionMenu(menu, x, y);
  }

  /** Resolve the nearest ancestor that owns a data-shot-id. */
  function findShotTarget(node) {
    let el = node;
    while (el && el !== document.body) {
      if (el.nodeType === 1 && el.hasAttribute && el.hasAttribute("data-shot-id")) {
        return el;
      }
      el = el.parentNode;
    }
    return null;
  }

  document.addEventListener(
    "contextmenu",
    function (ev) {
      const target = findShotTarget(ev.target);
      if (!target) {
        return;
      }
      const rawId = target.getAttribute("data-shot-id");
      if (!rawId) {
        return;
      }
      ev.preventDefault();
      openMenuFor(target, rawId, ev.clientX, ev.clientY);
    },
    false
  );

  // Dismiss handlers — keep them lightweight; menu is only briefly open.
  document.addEventListener("mousedown", function (ev) {
    const menu = document.getElementById(MENU_ID);
    if (!menu || menu.hidden) {
      return;
    }
    if (ev.target instanceof Node && menu.contains(ev.target)) {
      return;
    }
    hideMenu();
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      hideMenu();
    }
  });

  window.addEventListener("scroll", hideMenu, true);
  window.addEventListener("resize", hideMenu);
  window.addEventListener("blur", hideMenu);

  // Re-hide after htmx swaps — the old menu may still reference a stale
  // DOM node that no longer exists.
  if (document.body) {
    document.body.addEventListener("htmx:beforeSwap", hideMenu);
  }
})();
