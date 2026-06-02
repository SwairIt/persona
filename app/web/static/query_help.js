/*
 * Persona — query syntax help popover (v0.43).
 *
 * Vanilla ES2020. No frameworks.
 *
 * Watches the document for clicks on any element carrying the
 * `data-query-help-toggle` attribute (typically a small "?" button placed
 * next to a search input) and toggles a popover that summarises the
 * FTS5 query grammar plus Persona-specific `tag:` / `app:` / `date:`
 * prefixes. Escape closes the popover, as does clicking outside of it.
 *
 * Only one popover is ever live at a time. The popover is appended to
 * <body> and absolutely positioned just below the trigger so it floats
 * above any scroll container and never gets clipped by `overflow: hidden`
 * parents. Position is recomputed on resize / scroll while it's open.
 */
(function () {
  "use strict";

  const POPOVER_ID = "persona-query-help-popover";
  const POPOVER_WIDTH = 360;
  const VIEWPORT_MARGIN = 8;

  /** @type {HTMLElement | null} */
  let activePopover = null;
  /** @type {HTMLElement | null} */
  let activeTrigger = null;

  /**
   * Build the popover DOM once. The element is detached on close and
   * re-attached on open, so we always start from a fresh layout.
   *
   * @returns {HTMLElement}
   */
  function buildPopover() {
    const root = document.createElement("div");
    root.id = POPOVER_ID;
    root.className = "persona-query-help";
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-label", "Query syntax help");

    root.innerHTML = [
      '<div class="persona-query-help__header">',
      '<span class="persona-query-help__title">Query syntax</span>',
      '<button type="button" class="persona-query-help__close" aria-label="Close">×</button>',
      "</div>",
      '<div class="persona-query-help__body">',
      '<section>',
      '<h4>Full-text (FTS5)</h4>',
      '<dl>',
      '<dt><code>"exact phrase"</code></dt>',
      '<dd>Match the words in order.</dd>',
      '<dt><code>a AND b</code> &middot; <code>a OR b</code> &middot; <code>a NOT b</code></dt>',
      '<dd>Boolean operators. <code>AND</code> is implicit between bare words.</dd>',
      '<dt><code>NEAR(auth token, 5)</code></dt>',
      '<dd>Terms within N tokens of each other (default 10).</dd>',
      '<dt><code>auth*</code></dt>',
      '<dd>Prefix match. The <code>*</code> only works at the end.</dd>',
      '</dl>',
      '</section>',
      '<section>',
      '<h4>Persona filters</h4>',
      '<dl>',
      '<dt><code>tag:standup</code></dt>',
      '<dd>Only items tagged <code>standup</code>. Repeat to AND multiple tags.</dd>',
      '<dt><code>app:Slack</code></dt>',
      '<dd>Restrict to a captured application. Quote names with spaces: <code>app:"Visual Studio"</code>.</dd>',
      '<dt><code>date:2026-06-02</code></dt>',
      '<dd>ISO date. Use a range with two values: <code>date:2026-05-01..2026-05-31</code>.</dd>',
      '</dl>',
      '</section>',
      '<section>',
      '<h4>Examples</h4>',
      '<ul>',
      '<li><code>"deploy script" app:Terminal date:2026-05-30</code></li>',
      '<li><code>tag:meeting NEAR(anna roadmap, 8)</code></li>',
      '<li><code>auth* NOT logout app:Slack</code></li>',
      '</ul>',
      '</section>',
      "</div>",
    ].join("");

    root.addEventListener("click", function (ev) {
      const target = /** @type {HTMLElement} */ (ev.target);
      if (target.classList.contains("persona-query-help__close")) {
        closePopover();
      }
    });

    return root;
  }

  /**
   * Place the popover just below (or above, if no room) the trigger
   * element, clamped to the viewport.
   *
   * @param {HTMLElement} pop
   * @param {HTMLElement} trigger
   */
  function positionPopover(pop, trigger) {
    const rect = trigger.getBoundingClientRect();
    const scrollX = window.pageXOffset || document.documentElement.scrollLeft;
    const scrollY = window.pageYOffset || document.documentElement.scrollTop;
    const viewportW = document.documentElement.clientWidth;
    const viewportH = document.documentElement.clientHeight;

    // Prefer aligning the popover's right edge with the trigger's right
    // edge so it grows leftward — the `?` is usually near the right side
    // of the form, and growing rightward would push it off-screen.
    let left = rect.right - POPOVER_WIDTH;
    if (left < VIEWPORT_MARGIN) {
      left = VIEWPORT_MARGIN;
    }
    if (left + POPOVER_WIDTH > viewportW - VIEWPORT_MARGIN) {
      left = viewportW - VIEWPORT_MARGIN - POPOVER_WIDTH;
    }

    // Open downward by default; flip above if the popover would overflow
    // the bottom of the viewport and there's more room above.
    const popHeight = pop.offsetHeight || 280;
    let top = rect.bottom + 6;
    if (top + popHeight > viewportH - VIEWPORT_MARGIN && rect.top > popHeight + 6) {
      top = rect.top - popHeight - 6;
    }

    pop.style.left = (left + scrollX) + "px";
    pop.style.top = (top + scrollY) + "px";
    pop.style.width = POPOVER_WIDTH + "px";
  }

  /**
   * @param {HTMLElement} trigger
   */
  function openPopover(trigger) {
    if (activePopover) {
      closePopover();
    }
    const pop = buildPopover();
    document.body.appendChild(pop);
    activePopover = pop;
    activeTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");

    // Position after attach so offsetHeight is real.
    positionPopover(pop, trigger);

    window.addEventListener("resize", onReflow, { passive: true });
    window.addEventListener("scroll", onReflow, { passive: true, capture: true });
  }

  function closePopover() {
    if (!activePopover) return;
    activePopover.remove();
    activePopover = null;
    if (activeTrigger) {
      activeTrigger.setAttribute("aria-expanded", "false");
      activeTrigger = null;
    }
    window.removeEventListener("resize", onReflow);
    window.removeEventListener("scroll", onReflow, /** @type {any} */ (true));
  }

  function onReflow() {
    if (activePopover && activeTrigger) {
      positionPopover(activePopover, activeTrigger);
    }
  }

  document.addEventListener("click", function (ev) {
    const target = /** @type {HTMLElement | null} */ (ev.target);
    if (!target) return;

    const trigger = target.closest("[data-query-help-toggle]");
    if (trigger) {
      ev.preventDefault();
      if (activeTrigger === trigger) {
        closePopover();
      } else {
        openPopover(/** @type {HTMLElement} */ (trigger));
      }
      return;
    }

    // Click outside the popover closes it.
    if (activePopover && !activePopover.contains(target)) {
      closePopover();
    }
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && activePopover) {
      ev.preventDefault();
      closePopover();
      if (activeTrigger && typeof activeTrigger.focus === "function") {
        activeTrigger.focus();
      }
    }
  });
})();
