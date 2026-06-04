# Persona on iPhone Safari — UX assessment (v1.24)

Read-only review of the web shell from the perspective of an iPhone user. The
Windows server keeps capturing; the phone is a pure *consumer* of memory.

## 1. What already works (v1.24)

- **Responsive viewport meta** is set correctly in `app/web/templates/base.html:9`.
- **Responsive header (v1.17)** — `app/web/templates/base.html:101–242`:
  - `<md` (≤768px): hamburger opens a full-screen drawer with every nav link.
  - `md–lg`: core 5 nav items + “Ещё” dropdown.
  - Status pill compresses to icons-only below `md` (text labels live behind
    `hidden sm:inline`).
- **PWA manifest** at `app/web/static/manifest.json`: `display: standalone`,
  `theme_color #6366f1`, `start_url /timeline`, single 512×512 icon present at
  `app/web/static/icon-512.png` (verified, 4.9 KB).
- **Service worker** at `app/web/static/sw.js` (cache name `persona-v1.24`):
  network-first for pages + `/api/*`, cache-first for `/static/*` and CDN
  assets. Precaches Tailwind/htmx/Alpine/markdown-it so the shell renders
  offline.
- **SW registration** is guarded by `window.isSecureContext`
  (`base.html:18`) — works on the devtunnels HTTPS URL.
- **Theme=auto** follows iOS dark mode (`base.html:28–45`).
- **Dedicated phone view** at `GET /m` (`app/web/routes/mobile.py:17`) renders
  `templates/mobile.html` — text-only “today” list + search + yesterday’s
  digest. Independent of `base.html` and already touch-friendly.
- **Touch already accounted for** in scrubber CSS via `pointer-events: none`
  on the tooltip — at least it won’t hijack taps.

## 2. Critical breakages on iPhone Safari

Verified against actual source, not guessed:

1. **Timeline scrubber is hover-only** — `app/web/static/timeline_scrubber.js:52,69`
   binds only `mousemove` / `mouseleave`. iOS has no hover; the preview chip
   simply never appears. Used on `templates/day_scrubber.html`.
2. **Pixel-tiny touch targets in the header status pill**
   (`base.html:186–210`). The capture/mic/theme/hamburger buttons are
   `px-2 py-1` (~26–28 px tall) — below the iOS HIG 44 px minimum. On a
   normal-sized iPhone they’re hard to hit, especially the mic kill-switch
   which is the one button a phone user actually wants.
3. **Wide tables with no horizontal scroll wrapper** —
   `templates/memory.html:49–73` (micro-pins table, 5 cols) sits inside a
   simple bordered container, no `overflow-x-auto`. Same pattern repeats on
   `stats.html`, `llm_cost`, `quality_lab`, `audit`, `health_dashboard`, etc.
   On a 390 px viewport the table either overflows the page or wraps into an
   unreadable column.
4. **Keyboard-only surfaces are dead weight on iPhone**:
   - `?` cheatsheet — `static/shortcuts_overlay.js`.
   - Cmd/Ctrl+K command palette — `static/command_palette.js`.
   - Quick-pin `P` — `static/quick_pin.js`.
   - Search jump keys — `static/search_keyboard.js`.
   - These don’t *break* anything, but they ship JS + CSS to every phone
     load and there’s no equivalent touch affordance (e.g. a floating
     “command” FAB).
5. **`type="text"` everywhere a number is meant** — `templates/clipboard.html`
   is the *only* file using `inputmode=`. Number-ish fields on
   `app_retention.html`, `audio_settings.html`, `app_overrides.html` are
   plain text inputs → iOS shows the alphabetic keyboard instead of the
   numeric one. Minor, but admin-shaped pages so low priority.
6. **Header is dense.** Even after `v1.17`, the right-hand cluster on a
   390 px viewport has: heartbeat dot + status text + capture btn + mic btn +
   hamburger = 5 hit zones competing in one row. Easy to mis-tap the “●
   capture now” when reaching for the burger.
7. **`/mobile` is orphaned** — `templates/mobile.html` exists and is reachable
   at `/m`, but **nothing in the desktop shell links to it**
   (`Grep "/mobile"` in templates → 0 hits). A first-time iPhone user lands
   on the desktop layout by default.
8. **Modal/drawer doesn’t lock body scroll** (`base.html:215`). iOS Safari
   will happily scroll the page behind the drawer when fingers wander.
9. **Status pill has both an inline `:title` tooltip on the budget link
   (`base.html:181`) and on the mic/capture buttons** — `title` only fires on
   hover, so on iPhone these are invisible. The user has no way to know what
   the “●” button does.

## 3. PWA “Add to Home Screen” flow

- Manifest is valid; iOS Safari will offer “Add to Home Screen” from the
  share sheet.
- **Missing** (silently degrades, doesn’t fail):
  - No `<link rel="apple-touch-icon">` → iOS picks an automatic screenshot
    of the page as the home-screen icon instead of the supplied PNG.
    Confirmed by `Grep "apple-touch-icon"` → no matches.
  - No `<meta name="apple-mobile-web-app-capable" content="yes">` and no
    `apple-mobile-web-app-status-bar-style` → on tap-from-home the app
    opens in a Safari tab with full chrome instead of standalone.
  - Manifest has a single 512 px icon; iOS also prefers a 180×180 for the
    home screen and a maskable variant for proper rounded-corner cropping.
- Service-worker offline story is solid: `networkFirst` falls back to
  cached HTML, so toggling airplane mode keeps the last timeline view
  readable.

## 4. Use cases that genuinely make sense from iPhone

The phone is read-mostly. These flows are worth optimising:

1. **Ask** (`/ask`) — natural-language Q&A; the killer “mobile” feature
   because the answer is a single paragraph.
2. **Search** (`/search`) — quick lookup of “what was that thing I read
   Tuesday”.
3. **Today/Timeline** (`/`) — glance at what the Windows machine has
   captured today.
4. **Weekly / daily digest** (`/digest/weekly`, `/digest/daily/<day>`) —
   morning reading.
5. **Pause mic** — the *one* control the user actually needs from a phone
   (entering a meeting, picking up a call). Currently a tiny header
   button.
6. **Reminders / journal entry** (`/reminders`, `/journal`) — quick add
   from the couch.
7. **Pause / resume capture** — same logic as the mic toggle.

## 5. Use cases that DON’T make sense on phone

Don’t spend effort here; desktop is fine:

- Quality lab, OCR diff / retry / near-dup / vision admin.
- Tag rules, regex rules, phrase tags, app aliases, app groups, redaction.
- Capture blocklist, retention preview, storage report, vault, embeddings
  reindex, lang autodetect, OCR languages.
- Webhooks, API tokens, SMTP settings, audit, theme settings, doctor.
- Anything under `/settings/*` beyond a basic mic/capture toggle.
- Bulk select, bulk delete, bulk pin (multi-select UI is painful on touch).

Mark these `desktop-only` in the nav rather than polishing them.

## 6. Top 5 phone-specific improvements (ranked by ROI)

1. **Link `/m` from the desktop header on small viewports**, or just route
   `/` to `/m` when `User-Agent` matches mobile + width ≤ 640 px.
   `app/web/routes/mobile.py:17` already exists; surfacing it is a 5-line
   change to `base.html`. Single biggest UX win for zero design work.
2. **Add iOS PWA meta tags** in `base.html:7–13`:
   ```html
   <link rel="apple-touch-icon" href="/static/icon-180.png">
   <meta name="apple-mobile-web-app-capable" content="yes">
   <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
   <meta name="apple-mobile-web-app-title" content="Persona">
   ```
   Plus ship a 180×180 PNG and add it + a `maskable` variant to
   `manifest.json`. Makes Add-to-Home-Screen look native.
3. **Bump header touch targets** — change `px-2 py-1` to `p-2.5` (≈40 px) on
   the 5 buttons in `base.html:186–210`, and pull the mic toggle out into
   the mobile drawer as a full-width row. The mic kill-switch is the most
   useful phone control; it shouldn’t be a 26 px square.
4. **Add touch fallback to timeline scrubber** —
   `static/timeline_scrubber.js:52` should also listen for `touchstart` +
   `touchmove`, render the chip above the finger (not under it), and dismiss
   on `touchend`. Without this the whole /day_scrubber page is useless on
   phone.
5. **Wrap wide tables in `overflow-x-auto`** — at minimum
   `templates/memory.html:49`, `stats.html`, `audit.html`,
   `health_dashboard.html`. Two-character fix per file
   (`<div class="overflow-x-auto">` around `<table>`), turns broken layout
   into horizontal swipe.

(Bonus #6, cheap: `body { overscroll-behavior: contain }` + lock scroll
when the drawer is open.)

## 7. Verdict

**Not yet ready for daily iPhone use, but close.** The plumbing is right
(PWA manifest, SW, responsive shell, dedicated `/m` view) — the gap is
that the phone hits the *desktop* shell by default, lands on dense tables
and hover-only tooltips, and the one control a phone user actually
needs (mic kill-switch) is a 26 px square buried in a 5-button cluster.

**Single most important fix: route mobile UAs to `/m` (or surface a clear
“Mobile” link in the drawer).** Everything else — touch targets, PWA
icons, scrubber touch handler — is a polish pass on top of an existing
mobile-first page that nobody is being sent to.
