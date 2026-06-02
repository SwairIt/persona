# 🌅 Morning report — Persona

Fifty autonomous sessions: …v0.47 → v0.48 → v0.49 → **v0.50 MILESTONE** (half-way to v1, chained back-to-back). **Live on GitHub:** https://github.com/SwairIt/persona

## By the numbers (current)

- `version = "0.2.0"` in `pyproject.toml`
- **~135 source/template/SQL/doc files**, **~8 500 lines** of code & UI
- **22 HTTP route modules** mounted on a single FastAPI app
- **17 modules** in `app/` (settings, capture, dedup, ocr, search, embeddings, storage, workers, web, llm, analysis)
- **20+ test files**
- **0 git commits** — entirely up to you
- **0 network calls at runtime** (only optional outgoing call: BYO-LLM keys to your chosen provider)

## What landed in v0.50 MILESTONE (feature index + JSON query API + setup wizard)

Built via 3 parallel Workflow agents → sequential wire-up. **Half-way to v1.** First "meta" tick instead of just adding features — addresses *discoverability*, *programmability*, and *first-run UX*.

- 🗂️ **Feature index** — `/features` lists every route in the app with a one-line hint, grouped by category (timeline / search / capture / ocr / llm / export / share / admin / stats / settings / integrations). Type to filter. Programmatic at `/api/features.json` — useful for plugins or tooling that wants to know what's available.
- 🔎 **JSON query API** — POST `/api/query` accepts `{fts, app, date_from, date_to, tags, kinds, limit}` and returns mixed results: screenshots, notes, tags, days. Reuses every existing search helper under the hood — no duplicated SQL. `/api/query/example` is self-describing.
- 🧙 **One-shot setup wizard** — fresh installs are redirected to `/setup` by a tiny `SetupGateMiddleware` until they complete the form: theme, capture cadence, OCR languages, BYO LLM key, retention windows. API key goes into the v0.33 encrypted vault when `cryptography` is installed. Allow-list keeps `/api/*` and `/static/*` reachable so the redirect can't loop or break the bookmarklet.

## What landed in v0.49 (per-tag RSS + visual diff thumbs + per-app retention)

Built via 3 parallel Workflow agents → sequential wire-up. Last tick before the v0.50 milestone.

- 📡 **Per-tag RSS** — `/tags/{name}.rss` is a feed of the 50 most-recent shots tagged with that name. Plays nicely with `/search.rss` (per-query) and `/collection/{slug}.rss` (per-auto-collection) from earlier ticks. OCR snippets in descriptions pass through your v0.24 redaction rules.
- 🎨 **Visual diff thumbnails** — `/api/diff/{a}/{b}/thumb.png` returns a 320×180 PNG showing `PIL.ImageChops.difference(a, b)` with 2× contrast. v0.33's diff slider page now embeds it below the slider so you instantly see *where* the pixels changed.
- 🗂️ **Per-app retention overrides** — `/settings/app-retention` lets you set different warm/cold/delete cutoffs per `app_name`, plus a "Never delete" flag for apps you want to keep forever (VS Code, Linear, etc.). The retention worker checks the per-app row first and falls back to the global settings when columns are NULL.

## What landed in v0.48 (permalinks + reading-time per day + tag merge)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🔗 **Permalinks** — POST `/api/permalink` with any internal URL gets you back a short `/go/{slug}` redirect (8-char base36) you can share or stash in notes. Open-redirect-safe: only relative `/`-prefixed URLs accepted. `/permalinks` admin page shows all slugs + hits.
- 📖 **Reading-time per day** — `/stats/reading-time?day=YYYY-MM-DD` totals OCR words + note words across a day and reports "N minutes at 250 wpm" plus a per-app CSS bar chart. Encrypted notes excluded. JSON API at `/api/reading-time.json`.
- 🔀 **Tag merge** — `/admin/tag-merge` lets you merge tag `B` into tag `A`. Preview shows how many shots will move; confirm runs the atomic UPDATE + DELETE inside a transaction (INSERT OR IGNORE for shots that already had both). Action is recorded in the v0.36 audit log.

## What landed in v0.47 (notes day-timeline + dup suggestions + audit RSS)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🧵 **Per-day notes timeline** — `/notes/day/YYYY-MM-DD` shows the day's notes as a vertical timeline (left-edge line, dots, timestamps, body). Each note has an `#note-{id}` anchor for deep-linking. Markdown rendered when the optional `markdown` lib is installed; otherwise escaped `<pre>`.
- 👁️ **Possibly-related strip** — the screenshot detail page now lazily loads up to 4 similar shots: same `dedup_group_id` first, then nearest-Hamming pHash neighbours. Click any to jump there.
- 📡 **Audit-log RSS** — `/audit.rss` is your personal "what did the admin endpoints do?" feed. Loopback-only (so no random visitor reads your audit log), last 100 entries, RSS 2.0, properly XML-escaped. Drop into any feed reader pointing at `localhost`.

## What landed in v0.46 (tag colours + image zoom + day kanban)

Built via 3 parallel Workflow agents → sequential wire-up. First tick with **zero gap** to the previous one.

- 🎨 **Per-tag colour** — `/tags` page now has an `<input type="color">` next to every tag. Pick a hex, the chip background updates everywhere instantly via HTMX. Server validates `^#[0-9a-f]{6}$` so no surprises in CSS.
- 🔍 **Image viewer zoom & pan** — the screenshot detail page now supports wheel-zoom around the cursor (1× → 8×), click-drag panning, double-click reset, and two-finger pinch on touch devices. Pure CSS transform + vanilla JS, no library.
- 📋 **Day kanban view** — `/kanban/{day}` groups a day's shots into horizontal columns by `app_name`. Each card shows the app, shot count, and a vertical strip of thumbnails. Useful for "what apps did I bounce between?" at a glance.

## What landed in v0.45 (app icon cache + encrypted notes + retention preview)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🅰️ **Per-app icon cache** — `/app-icon/{name}.png` returns a deterministic 64×64 PNG: two-letter initials on a colour derived from `sha256(app_name)`. Cached in DB, served with `Cache-Control: max-age=86400`. Drop-in for timelines / tag pages / time-on-app — each app instantly recognisable.
- 🔐 **Encrypted note bodies** — opt-in per note. POST `/api/notes/{id}/encrypt` with master password → body wiped, ciphertext stored (Fernet + PBKDF2-HMAC-SHA256 100k iters, per-note salt). POST `/api/notes/{id}/decrypt` returns the plaintext for one-time display. Encrypted notes are filtered out of FTS search. Every decrypt is recorded in the v0.36 audit log.
- 🧮 **Retention preview** — `/admin/retention-preview` is a dry-run of the next retention worker tick: how many shots would demote warm/cold, how many hard-delete, sample thumbnails per bucket, MB freed estimate. Banner clearly says "no changes made". Catches "oh no I set the wrong cutoff" mistakes before they happen.

## What landed in v0.44 (webhook event filters + OCR near-dup admin + public day)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🪝 **Webhook event filters** — each webhook row now has an `event_types` column. Default `*` keeps the old "fire on everything" behaviour. Set it to a comma-separated list (`screenshot.captured, ocr.done`) or a glob (`screenshot.*`) to subscribe to a specific subset. Editable per-webhook from the settings page.
- 👯 **OCR near-duplicate admin** — `/admin/ocr-near-duplicates` runs a Jaccard similarity scan over OCR token sets and lists clusters above your threshold (default 0.85) with side-by-side thumbnails and a Keep-A / Keep-B / Keep-both decision. Deleting an item routes through the v0.40 recycle bin so nothing is hard-deleted.
- 🌍 **Public day opt-in** — `/admin/public-days` lets you publish a single day under a friendly slug. `/public/day/{slug}` renders that day chrome-free for sharing. Sensitive content is filtered server-side: shots tagged `private`/`confidential` are skipped, OCR text passes through your v0.24 redaction rules.

## What landed in v0.43 (query help + context menu + per-shot share link)

Built via 3 parallel Workflow agents → sequential wire-up.

- ❔ **Query syntax help** — click the `?` next to the search box on `/search` (or anywhere a search input exists) to see a popover cheatsheet for FTS5 syntax: `"phrase"`, `AND` / `OR` / `NOT`, `NEAR(a b 5)`, `prefix*`, plus Persona-specific `tag:standup`, `app:Slack`, `date:2026-06-02`.
- 🖱️ **Right-click context menu** — right-click any screenshot thumbnail to get Pin / Favourite / Open / Add tag (prompt) / Add to collection / Delete (→ recycle bin). Green flash on success, red on failure. Pure vanilla JS, closes on outside click or Escape.
- 🔗 **Per-screenshot share link** — `/screenshot/{id}/share` creates an HMAC-signed share URL with configurable TTL (1 hour to 30 days). The public view at `/shot/share/{id}/{token}` strips all Persona chrome — just the image, caption, and a small "Generated by Persona" footer. Revoke any token from the same page; invalid/expired tokens return `410 Gone`.

## What landed in v0.42 (day scrubber + OCR retry queue + day collage PNG)

Built via 3 parallel Workflow agents → sequential wire-up.

- ▶️ **Day scrubber video player** — `/scrubber/{day}` lets you scrub through a whole day's screenshots like a video: big image at top, slider at the bottom, play/pause/prev/next + 0.5×/1×/2×/5× speed. Arrow keys step, Space toggles play. Great for the "what was I doing at 14:32?" question.
- 🔁 **OCR retry queue** — `/admin/ocr-retry` lists shots with empty or low-confidence OCR. Filter pills (empty / low-conf / both), checkbox selection, "Requeue selected" or "Requeue all (max 1000)". Sets `ocr_done=0` so the existing OCR worker picks them up on the next tick.
- 🖼️ **Per-day collage PNG** — `/export/collage.png?day=YYYY-MM-DD` produces a single PNG with a 4×N grid of that day's top thumbnails (default 24 tiles, 320 px each, configurable). Shareable image artefact. CLI: `persona-cli export-collage --day YYYY-MM-DD --out FILE`.

## What landed in v0.41 (search facets + drag-to-tag + browser bookmarklet)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🎚️ **Search facets** — `/search` now has a collapsible Filters panel: filter by app (top 50 most-active in a dropdown), date range, and tag (with autocomplete from your existing tags). All compose with the free-text FTS5 query. Direct API at `/api/search/facets.json` for tooling.
- 🖱️ **Drag-to-tag** — pure HTML5 drag-and-drop. Drag any tag chip onto a screenshot thumbnail to apply that tag. Green flash on success, red on failure. No framework, all vanilla.
- 🔖 **Browser bookmarklet** — `/bookmarklet` shows a draggable javascript: URL. Click it on any page and the current URL, title, and selected text get POSTed to `/api/bookmarklet/capture` and become a new Persona note (`# {title}\n\n{url}\n\n> {selection}`). CORS-enabled by design since the bookmarklet runs on third-party origins.

## What landed in v0.40 milestone (SSE live status + OCR .txt + undo bin)

Built via 3 parallel Workflow agents → sequential wire-up. v0.40 is the 19th version shipped in this autonomous loop.

- 📡 **Live SSE status pill** — `/events` is now a Server-Sent Events stream. The status pill in `base.html` updates the moment something changes (no more polling lag). Pushes status snapshots every 2s plus worker heartbeats whenever a worker beats. Auto-reconnect on disconnect; gracefully no-ops if the browser lacks `EventSource`.
- 📝 **Per-day OCR .txt export** — `/export/ocr.txt?day=YYYY-MM-DD` produces a plain-text dump of all that day's OCR (one block per shot, `===` delimiters, app+timestamp header). Perfect for `grep`, `fzf`, or `rg` workflows. CLI: `persona-cli export-ocr-txt --day YYYY-MM-DD --out FILE`.
- ♻️ **Undo bin (soft-delete)** — bulk-delete + screenshot delete now move items to `/recycle` for 7 days (configurable via `PERSONA_RECYCLE_RETENTION_DAYS`, range 1–90) before hard deletion. Restore any item with one click. retention worker auto-purges expired entries. Atomic insert-then-delete inside a transaction so nothing slips through the cracks.

## What landed in v0.39 (keyboard cheatsheet + OCR language stats + archive ZIP)

Built via 3 parallel Workflow agents → sequential wire-up.

- ⌨️ **Keyboard shortcut cheatsheet** — press `?` from any page (when not typing in a field) to see all available shortcuts: `?` for help, Cmd/Ctrl+K for palette (v0.38), `/` to focus the search box on /search, and `g`+letter sequences for go-to navigation (`g t` → timeline, `g s` → search, `g h` → heatmap, `g f` → focus mode). Multi-key sequences time out after 1.5s.
- 🔤 **OCR language statistics** — `/stats/ocr-languages` measures the actual mix of writing systems in your captured text (Cyrillic vs Latin vs CJK vs digits vs other). Per-language top apps so you can see e.g. "Russian comes from Telegram, English from VS Code". JSON API at `/api/ocr-languages.json`.
- 📦 **Archive ZIP bundle** — `/export/archive.zip?days=7&thumbs=1` packages your settings (no secrets, via v0.37's exporter), screenshots + notes JSON, and last N days of thumbnails into a single deflated zip with a README manifest. CLI: `persona-cli archive --days 7 --out FILE`. Perfect for "I want everything on a stick".

## What landed in v0.38 (Cmd+K palette + shot of the week + stats CSV)

Built via 3 parallel Workflow agents → sequential wire-up.

- ⌨️ **Cmd+K command palette** — hit Cmd+K (or Ctrl+K on Windows/Linux) from any page to open a fuzzy launcher with ~30 top routes plus your saved searches and auto-collections. Arrow keys + Enter to navigate, Esc closes. Recent routes persist in localStorage. Pure vanilla JS, no framework — opens instantly.
- 🏆 **Screenshot of the week** — `/shot-of-the-week` picks a curated highlight from last week. Score = pinned×5 + favourited×3 + tag_count + annotation_count. Shows the score breakdown so you understand why this shot won. Falls back to shot-of-the-day if the past week had no signals.
- 📑 **Stats CSV export** — `/export/stats.csv?days=90` produces a one-row-per-(date, app) CSV with shot count, active seconds, idle seconds, OCR character total, and TL;DR-cached flag. Drop into pandas / Excel for offline analysis. CLI also: `persona-cli export-stats-csv --days N`.

## What landed in v0.37 (settings backup JSON + heartbeat dashboard + markdown inbox)

Built via 3 parallel Workflow agents → sequential wire-up. 20th milestone tick.

- 💾 **Settings backup as JSON** — `/settings/backup` exports your full configuration (kv_setting, redaction rules, auto-collections, OCR skip-list, phrase tags, saved searches, note templates, webhook configs, quiet hours, etc.) as a single JSON file. Import on another machine with merge or replace. Secrets (webhook secret, vault ciphertext) are deliberately excluded. CLI: `persona-cli export-settings --out` / `import-settings --in [--replace]`.
- ❤️ **Worker heartbeat dashboard** — `/admin/health` shows every background worker's last tick + status with colour-coded freshness (green <2 min, amber <10 min, red older). Every worker (capture, OCR, retention, embeddings, digest schedulers, clipboard) calls `beat(name)` once per loop iteration. JSON API at `/api/health.json` — easy to wire to an external uptime monitor.
- 📥 **Markdown inbox** — drop a `*.md` file with optional YAML-ish frontmatter (`title:`, `tags: a, b`) into `data/inbox/` and Persona auto-imports it as a note. Successful files move to `data/inbox/processed/`, parse failures to `data/inbox/failed/` with a sibling `.error.txt`. Toggle with `PERSONA_INBOX_ENABLED=false`.

## What landed in v0.36 (Pomodoro focus mode + audit log + per-day TL;DR)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🍅 **Focus-mode (Pomodoro)** — `/focus` lets you start a 25-min work / 5-min break cycle (customisable). Big countdown clock with a CSS progress ring; ends with a Web Audio API beep. Sessions are recorded so you can review streaks. JS reads `started_at + work_minutes` so the timer survives reload.
- 📋 **Audit log** — every destructive admin action (bulk-delete, token issuance/revoke, vault set/get/delete) is recorded with timestamp, actor, target, and success flag. `/audit` page has substring filtering + pagination. Secrets are never logged — only the key *name* for vault operations.
- ✏️ **Per-day TL;DR** — `/api/day-tldr.json?day=YYYY-MM-DD` returns a one-sentence summary of a day (≤30 words) via your BYO LLM. Cached in `day_tldr` table so it's free after the first generate. POST `/api/day-tldr/{day}/regenerate` forces a fresh take. Returns `missing_config` status when no LLM key is set — never blocks render.

## What landed in v0.35 (clipboard history + OCR confidence overlay + .ics export)

Built via 3 parallel Workflow agents → sequential wire-up.

- 📋 **Clipboard history capture** — opt-in (`PERSONA_CLIPBOARD_HISTORY_ENABLED=true`). A background worker polls `CF_UNICODETEXT` every 2s on Windows; new (SHA-256 deduped) text snippets get stored alongside screenshots and pass through your v0.24 redaction rules so secrets get masked. `/clipboard` lists history with a LIKE search.
- 🎯 **OCR per-word confidence overlay** — `/screenshot/{id}/overlay` shows the screenshot with every OCR word as a coloured box: green ≥ 80, amber 50–79, red < 50 confidence. Click any word to jump to `/search?q=word`. The OCR worker writes `image_to_data` boxes alongside the text so you can see exactly where Tesseract was unsure.
- 📅 **iCalendar (.ics) export** — `/export/calendar.ics?days=90` produces a stdlib-only iCal 2.0 feed with one all-day event per active day (shot count + top 3 apps). Drop into Google / Apple Calendar to get a retrospective view of when you were heads-down on what.

## What landed in v0.34 (weekly stats PDF + OCR diff viewer + API tokens)

Built via 3 parallel Workflow agents → sequential wire-up. **Code now lives on GitHub** (`SwairIt/persona`) — pushed mid-tick.

- 📊 **Weekly stats PDF** — `/export/weekly-pdf?week=YYYY-MM-DD` produces a 5-page Mon–Sun summary: totals + streak, daily bar chart, top apps with hours, top keywords, and a thumbnail mosaic of the week's 12 most-recent shots. CLI: `persona-cli export-week-pdf --week YYYY-MM-DD --out file.pdf`.
- 🔍 **OCR diff viewer** — `/diff/ocr/{id_a}/{id_b}` shows a textual diff of two screenshots' OCR (`difflib.HtmlDiff` colouring), with unified / side-by-side toggle. Great for seeing what changed in a window between two captures.
- 🔑 **API token bearer auth** — `/settings/api-tokens` issues bearer tokens (one-time-shown raw value, only the SHA-256 hash is stored). `Authorization: Bearer <token>` on `/api/*` paths populates `request.state.scopes`. Default is permissive (no token required, backwards-compatible); set `PERSONA_API_AUTH_REQUIRED=true` to lock things down.

## What landed in v0.33 (tag trend sparklines + encrypted KV vault + diff slider)

Built via 3 parallel Workflow agents → sequential wire-up.

- 📈 **Per-tag trend sparklines** — `/tags/{tag}/trend` shows a 30-day sparkline of how often that tag appears, plus a date/count table. JSON API at `/api/tags/{tag}/trend.json`. Useful for tracking how often "standup" or "blocker" actually fired this month.
- 🔐 **Encrypted KV vault** — `/vault` stores API keys / webhook secrets / SMTP password under a master password. Fernet symmetric encryption, PBKDF2-HMAC-SHA256 with 100k iters and per-key salt. List of stored *names* is visible; values require master password to decrypt. Needs `[backup]` optional dep (cryptography). ⚠️ Heads-up: this re-uses the `/vault` URL — if you had any custom vault routes from a prior version, double-check before deploying.
- 🎚️ **Screenshot diff slider** — `/diff/{id_a}/{id_b}` overlays two screenshots with a draggable vertical handle that reveals before/after. Pure CSS clip-path + 1-line vanilla JS, no framework. Great for "what did I change between 10:00 and 11:00?". `/diff/random` picks two random shots for demo.

## What landed in v0.32 (day PDF export + theme switcher + adaptive cadence)

Built via 3 parallel Workflow agents → sequential wire-up.

- 📄 **Day PDF export** — `/export/pdf?day=YYYY-MM-DD` produces a printable PDF of any day: title + totals on page 1, then thumbnail + caption + OCR preview per screenshot, then the day's notes. Also from CLI: `persona-cli export-day-pdf --day YYYY-MM-DD --out file.pdf`. Needs `uv pip install reportlab` (reports `missing_dep` until installed).
- 🎨 **Theme switcher** — `/settings/theme` lets you pick dark / light / auto (auto follows `prefers-color-scheme`). Stored in `kv_setting`, applied server-side via a new `get_theme()` Jinja global so the choice survives reloads with no flash.
- ⚡ **Adaptive capture cadence** — capture interval now adapts to your activity: <30 s idle → min interval (default 30 s); 30-120 s → base interval; >120 s idle → grows toward max (default 600 s). Composes with v0.26's battery slowdown (adaptive first, then battery multiplier). Saves disk while you're AFK without missing actively-used moments.

## What landed in v0.31 (idle stats + phrase auto-tag + SMTP digest)

Built via 3 parallel Workflow agents → sequential wire-up.

- 💤 **Idle-time stats** — `/idle?day=YYYY-MM-DD` splits each day into active vs idle time using your `idle_seconds` (Win32 `GetLastInputInfo`) plus the same 5-min-gap rule as v0.29's time-on-app. Big H:MM:SS for each bucket, ratio bar, first/last capture times.
- 🏷️ **OCR phrase auto-tag** — `/settings/phrase-tags` adds literal multi-word rules like `"daily standup" → #standup`. Worker applies them right after OCR + redaction. Different from v0.21's regex auto-tag (these are exact phrases, no regex syntax). Toggle case-sensitivity per rule.
- 📧 **SMTP digest delivery** — `/settings/smtp` configures your own SMTP server (Gmail, Yandex, Migadu, etc.) and Persona can email your daily/weekly LLM digests to yourself. Opt-in (`smtp_enabled=true`), password masked in the UI, test-send button. Needs `uv pip install aiosmtplib` — Persona reports `missing_dep` status until installed.

## What landed in v0.30 (webhook HMAC + bulk-delete + hour histogram)

Built via 3 parallel Workflow agents → sequential wire-up. v0.30 is the 10th milestone in this autonomous loop.

- 🔐 **Webhook HMAC signing** — every outgoing webhook now ships with `X-Persona-Signature: sha256=…` and `X-Persona-Timestamp: …` headers. Each webhook row gets its own auto-generated `secrets.token_urlsafe(32)` secret on first use. `docs/WEBHOOK_SIGNING.md` has receiver-verify recipes for Python and Node (constant-time compare + replay protection).
- 🗑️ **Bulk-delete** — `persona-cli delete --query "old screenshot stuff" --confirm` deletes matching shots from CLI; without `--confirm` it dry-runs and prints the count. `/admin/bulk-delete` web page does the same with an HTMX preview + HMAC-token-protected confirm step (you can't accidentally delete by mashing buttons). Cascades to FTS index + thumbnail files.
- 📊 **Hour-of-day histogram** — `/hours?days=30` shows when you actually capture — 24-bar SVG chart with peak hour labelled. JSON API at `/api/hours.json`. Useful for spotting your real working windows.

## What landed in v0.29 (time-on-app + OCR language switcher + favourites)

Built via 3 parallel Workflow agents → sequential wire-up.

- ⏱️ **Time-on-app dashboard** — `/time-on-app?day=YYYY-MM-DD` sums up active time per app by walking consecutive captures. Gaps over 5 min count as idle so AFK time doesn't inflate any app's number. Table shows H:MM:SS, shot count, and a horizontal bar bar (CSS-only, no JS). `/time-on-app/summary?days=7` aggregates across a window.
- 🌐 **OCR language switcher** — `/settings/ocr-languages` lists every Tesseract language pack installed on your machine. Tick the boxes and save; the OCR worker picks up the new `eng+rus`-style string with a 60-second TTL cache. No need to restart.
- ⭐ **Favourites / star** — toggle a ★ on any shot via `POST /api/screenshot/{id}/favourite`; `/favourites` shows the starred grid, newest-first. Separate from pin: pin protects from auto-demotion, favourite is just a personal bookmark.

## What landed in v0.28 (calendar heatmap + top keywords + shot of the day)

Built via 3 parallel Workflow agents → sequential wire-up.

- 📅 **Calendar heatmap** — `/heatmap` shows 365 days of capture activity as a GitHub-style 53×7 SVG grid, with emerald cells bucketed by activity level (pct33/66/90). Hover any cell for date + shot count. JSON API at `/api/heatmap.json`.
- ☁️ **Top keywords of the week** — `/keywords?days=7&n=30` extracts the most frequent words from your OCR text + notes, filters out ~150 stopwords (English, Russian, technical noise), and renders them as a size-weighted tag cloud. Click any word to jump to `/search?q=...`. Configurable window (7 / 30 / 90 days) and count (15 / 30 / 50).
- 🎲 **Screenshot of the day** — `/shot-of-the-day` picks one shot from the last 90 days, deterministically seeded by today's date — same shot all day, different shot tomorrow. Bounded to 5000 candidates so memory stays flat.

## What landed in v0.27 (annotations + saved searches + daily streak)

Built via 3 parallel Workflow agents → sequential wire-up.

- 💬 **Per-screenshot annotations** — leave free-form commentary on any single shot. List/add/delete via `/api/screenshot/{id}/annotations` (HTMX-ready). Different from OCR text (auto-extracted), notes (global), and tags (categorical). Cascades on shot deletion.
- 🔖 **Saved search bookmarks** — pin queries you run often. `/searches` page lists them; clicking one (`/searches/{slug}`) redirects to `/search?q=...`. Separate from the auto-tracked search history added in v0.21 — these are explicit pins, not last-N-recent.
- 🔥 **Daily-capture streak** — `/streak` page shows current consecutive-days count, longest run ever, last capture date, and today's shot count. JSON API at `/api/streak.json`. Zero-state on empty DB.

## What landed in v0.26 (lock-aware pause + power-aware capture + notes FTS)

Built via 3 parallel Workflow agents → sequential wire-up. Privacy + battery-life focus this tick.

- 🔒 **Lock-aware capture pause** — when Windows is locked (Win+L), the capture loop detects the session state via WTS APIs and skips the iteration. No more useless lock-screen screenshots. On by default; toggle with `PERSONA_LOCK_AWARE_PAUSE_ENABLED=false`.
- 🔋 **Power-aware capture cadence** — on battery, capture interval is multiplied (default 3×) — fewer shots, less CPU drain. Below `battery_critical_pct` (default 15%), capture pauses entirely. Settings: `PERSONA_BATTERY_AWARE_ENABLED`, `PERSONA_BATTERY_CAPTURE_MULTIPLIER`, `PERSONA_BATTERY_CRITICAL_PCT`. Desktop machines (no battery) unaffected.
- 🔍 **Notes FTS search** — `/notes/search?q=...` searches across all your notes with `<mark>`-highlighted snippets, ranked by bm25. JSON API at `/api/notes/search.json` for integrations. FTS5 virtual table kept in sync via triggers (insert/update/delete).

## What landed in v0.25 (image-region blur + storage report + notes templates)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🖼️ **Physical image-region blur** — opt-in (`PERSONA_IMAGE_BLUR_ENABLED=true`). After OCR, the worker uses Tesseract's word-level bounding boxes to find regions matching your redaction patterns (emails, tokens, credit cards — same rules as v0.24's text redaction) and Gaussian-blurs those pixels in the saved image. The image at `/screenshot/{id}` now hides the secret visually, not just in search.
- 📊 **Per-day storage report** — `/storage-report` shows the last 30 days of disk usage: shots taken, thumbnails MB, OCR text KB, total MB. Pure-SVG sparkline (no JS), days over 4 MB highlighted amber, under 4 MB green. Directly tracks your 2–4 MB/day target.
- 📝 **Notes templates** — `/notes/templates` ships with three starters (Daily standup, Meeting/1:1, Bug investigation). Add your own with a slug + title + Markdown body. `GET /notes/templates/{slug}/apply` returns the body for one-tap paste into any note textarea.

## What landed in v0.24 (bulk-tag CLI + OCR redaction + RSS-per-collection)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🏷️ **Bulk-tag CLI** — `persona-cli tag --add work --query "GPT-4 OR Claude" --limit 1000` applies a tag to every screenshot whose FTS5 MATCH succeeds. `--dry-run` previews the count. `persona-cli untag --remove TAG --query Q` does the inverse. Creates tag rows on the fly.
- 🔒 **OCR text redaction** — `/settings/redaction` lets you add regex rules. The OCR worker masks every match with `***` before indexing — secrets stay out of FTS5 and out of search results. Ships with starter rules for email, credit-card numbers, and `Bearer …` tokens (all toggleable). The image itself is untouched.
- 📰 **RSS per auto-collection** — `/collection/{slug}.rss` feeds the latest 50 screenshots for any auto-collection rule (created via v0.23's `/collections`). Public collections are reachable from anywhere; private ones only from loopback. RSS 2.0, RFC-822 pubDate, XML-escaped — drop it into any feed reader.

## What landed in v0.23 (encrypted backup CLI + auto-collections + OCR skip-list)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🔐 **Encrypted backup/restore CLI** — `persona-cli backup --out FILE [--days 30]` packs the SQLite DB (after a WAL checkpoint) plus the last N days of thumbnails into a Fernet-encrypted tarball. Key is PBKDF2-HMAC-SHA256 from the passphrase (100k iters, random salt). `persona-cli restore --in FILE [--yes]` round-trips it back. Passphrase reads from `--password` or `PERSONA_BACKUP_PASSWORD`. Needs `uv pip install -e .[backup]` (cryptography optional dep).
- 🏷️ **Tag-driven auto-collections** — `/collections` page lets you bind a URL slug to a tag. Visiting `/collection/{slug}` renders every screenshot currently carrying that tag — membership is computed on read, so newly-tagged shots show up instantly. Public rules reachable from anywhere, non-public restricted to loopback. Slugs must match `^[a-z0-9-]{1,40}$`.
- 🚫 **Per-app OCR skip-list** — `/settings/ocr-skip` page lets you blacklist noisy apps (terminals, fullscreen video, games). Worker short-circuits OCR for those apps, marks the row done with empty text. Form pre-fills suggestions from `DISTINCT app_name` in your DB. Reduces FTS table bloat and saves CPU.

## What landed in v0.22 (doctor + weekly digest + capture-now CLI)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🩺 **persona-doctor diagnostic CLI + web page** — `persona-cli doctor` runs 12 sanity checks (Python version, SQLite + FTS5, data-dir writable, DB integrity via PRAGMA, Tesseract version, fastembed installed, BYO LLM configured, disk free, thumbnails size, recent capture, schema version) with coloured PASS/WARN/FAIL output and proper exit code. Same data renders at `/doctor` with a "What does this mean?" expander.
- 🗓️ **Weekly LLM digest scheduler** — opt-in (`PERSONA_WEEKLY_DIGEST_ENABLED=true`, `PERSONA_WEEKLY_DIGEST_HOUR_LOCAL=8`). Every Monday at the configured hour, summarises the previous Mon-Sun via BYO LLM into 250-400 words with structured sections (Big themes / Notable moments / What I shipped). Stored at `/digest/weekly-archive/{Monday}`.
- 📸 **`persona-cli capture`** — single-shot capture from the terminal. `--app NAME` overrides window detection, `--quiet` prints only the integer id (pipe-friendly). New `scripts\\capture_now.bat` and `docs\\CAPTURE_HOTKEY.md` show how to bind it to AutoHotkey v2 / PowerToys Run / a Windows shortcut for one-tap snapshots.

## What landed in v0.21 (archive search + regex auto-tag + search history)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🗄️ **Archive browse / search** — `/archive/browse` (recent) and `/archive/search?q=` (FTS5) now expose the cold-storage satellite SQLite that the archive worker spins up after 180 days. No thumbnails, just text + metadata.
- 🤖 **Regex auto-tag rules** — `/regex-rules` admin: define `/invoice/i` → tag "invoice"; whenever OCR finishes on a screenshot whose text matches an enabled rule, the tag gets auto-attached. Rule rows track `match_count` and `last_matched_at`. Live regex tester on the same page (debounced Alpine).
- 🕰️ **Search history** — every successful query is recorded (top 50 by recency); `/search` now shows "Recent" chip-row above the results. Per-chip `×N` use-counter, tooltip with last_used_at. "clear history" link.
- 🧪 Tests for regex CRUD/validation/apply-creates-tags/disabled-skipped + search-history record/list/clear + archive pages + regex test endpoint.

## What landed in v0.20 (share-collection + OCR reset + webhook test-fire)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🔗 **Shareable collection link** — `POST /api/share/collection?screenshot_ids=...&title=...&ttl_hours=24` returns a signed URL like `/share/collection/<token>` that renders a tiny gallery page. Reuses the same HMAC machinery as the single-screenshot share. Persisted in new `share_collections` table; respects `expires_unix`.
- 🔄 **OCR reset admin** — `/ocr-admin` page with status counters and three big buttons (reset skipped / failed / all). Also a `POST /api/screenshots/{id}/reset-ocr` for single-row reset. Plus a `persona-cli reset-ocr [--scope skipped|failed|all]` subcommand.
- 🧪 **Webhook test-fire** — every row on `/webhooks` now has a "test" button that fires a synthetic payload via the existing dispatcher. Useful for verifying a subscriber endpoint without waiting for a real event.

## What landed in v0.19 (CLI + quiet hours + reminder→screenshot)

Built via 3 parallel Workflow agents → sequential wire-up.

- 🖥 **Standalone CLI** — `python -m app stats|search|export-day|vacuum-db|ocr-status` (also `persona-cli` after `uv sync`). Query the memory from terminal without launching the web app. `vacuum-db` reports bytes reclaimed; `export-day` dumps the same markdown as `/api/export/journal.md`; `search` runs FTS5 with truncated snippets.
- 🌙 **Quiet hours** (`/quiet-hours`) — weekly recurring schedule for auto-pausing capture (e.g. Mon-Fri 23:00-07:00, weekends off). Capture loop checks at every tick, increments idle-skip counter without flipping the visible Paused state.
- ⏰ **Reminder ⇄ screenshot link** — on any screenshot detail page, a small "Remind me" widget creates a tomorrow-by-default reminder attached to that screenshot. The reminder row gains a 🖼 #N link back to the moment. New `POST /api/screenshots/{id}/remind` endpoint.
- 🧪 Tests for quiet-hours CRUD/validation/is_quiet_now match+miss/empty + reminder-screenshot linking + page render + remind API + CLI import smoke.

## What landed in v0.18 (custom ranges + per-app cadence + diff picker)

Built via 3 parallel Workflow agents → sequential wire-up.

- 📅 **Custom date-range timeline** (`/range`) — pick any since/until window up to 90 days. Quick-presets: Last 7 / Last 30 / This week / This month. Silently swaps reversed input.
- 🐢 **Per-app capture-interval override** (`/app-overrides`) — give Slack a 2-second cadence and Spotify a 30-second one. The capture loop reads the table after determining the foreground app and adjusts its sleep accordingly.
- ↔️ **Diff picker** (`/diff-picker?left=ID`) — pick a screenshot, get a grid of same-app same-day candidates to compare. Each thumbnail is a click-through to `/diff?left=X&right=Y`.
- 🧪 Tests for override CRUD + range edge cases (default, invalid date, reversed swap, with data) + diff picker (empty + with left).

## What landed in v0.17 (bulk pin + RSS-per-search + sparklines)

- 📌 **Bulk-pin from search** — checkbox + "Pin" button next to bulk-tag, pins all selected.
- 📡 **Per-saved-search RSS feed** — `/feeds/saved-search/{id}.rss`. Each saved search now has a tiny RSS icon next to it on `/tags`. Drop into your reader to watch a query.
- 📈 **14-day sparkline on Apps index** — every app row shows a tiny vertical bar chart of last-14-day activity. Filter input still works.
- 🧪 Tests for bulk-pin (happy + validation) + saved-search RSS (happy + 404).

## What landed in v0.16 (navigation + bulk tag)

- ⬅️ ➡️ **Prev / next screenshot navigation** — buttons + ← → keyboard on every screenshot detail page (input fields don't grab the keys). "back to YYYY-MM-DD →" link returns to that day's timeline.
- 🏷️ **Bulk-tag from search** — checkbox on every search result + sticky bulk-tag input. Type a name, hit Apply, the tag is auto-created if needed and bound to every selected screenshot.
- 🧪 Tests for neighbour lookup (happy / first / last / missing) + bulk-apply happy path + validation (empty tag / empty ids / non-numeric).

## What landed in v0.15 (day-journal export + about page + tag chips)

- 📓 **Per-day journal Markdown** — `/api/export/journal.md?date=YYYY-MM-DD` bundles the day's auto-digest, focus sessions, every note, and top apps into one portable .md. Download link added to the weekly digest header.
- 🪪 **`/about` feature dashboard** — pills for each optional bit (OCR, semantic, BYO LLM, auto-digest, tiered retention, smart thumb, multi-monitor, archive, vault, webhooks), tally cards, and a grid of all the hidden routes that aren't in the nav.
- 🏷️ **Tag chips on timeline cards** — every screenshot card on the timeline now shows up to 3 colour-coded tag chips with overflow indicator.
- 🧪 Tests for /about render, journal export edge cases, bulk get_tags_for_many.

## What landed in v0.14 (rename apps + tag colour edit + saved-search alerts)

- 🏷️ **Process rename** — `/process-remap` lets you map `whatever.exe` → "Pretty Name". Built-in mappings (Chrome, VS Code, …) still apply for everything you don't override. Suggested list shows your top processes that don't yet have a custom rename. Applied at capture time, future captures only (old screenshots keep their old `app_name`).
- 🎨 **Tag colour picker** — clickable colour swatch next to each tag on `/tags` updates the colour live via `/api/tags/{id}/color` (hex validated).
- 🔔 **Saved-search "N new" badge** — each saved search on `/tags` shows how many new matches landed since you last opened it. Click marks as seen.
- 🧪 Tests for process remap CRUD + tag colour set/clear/reject-invalid + saved-search new-count / mark-seen lifecycle.

## What landed in v0.13 (tag admin + per-tag stats + reading export)

- 🏷️ **Tag rename / merge / delete** — every tag's `/tags/{id}` page now has a Manage section with three controls. Rename auto-merges into an existing target with the same name. Merge moves all bindings + deletes the source. Delete cascades to remove the tag from every screenshot.
- 📈 **Per-tag stats** — same tag detail page now shows a 60-day activity bar chart + "often appears with" co-tag chart.
- 📄 **Reading-list markdown export** — `↓ markdown` button on `/reading` produces a portable `.md` file.
- 🧪 Tests for rename / merge / merge-into-existing / merge-self / delete / co-tag-counts / per-day.

## What landed in v0.12 (auto-tag + advanced filters)

- 🏷️ **AI-suggested tags** — the Tags section of every screenshot detail page now has a `✨ Suggest tags via AI` button. The LLM reads OCR+window title and proposes 3-5 short tags. Tap to select, then Apply. Robust JSON-extraction parser handles LLM preamble, lowercases, dedups, supports Russian and English. Skipped for private screenshots.
- 🔍 **Advanced search filters** — `/search` now sports `tier` (any / hot / warm / cold / pinned), exact `tag`, exact `app`, and date-range pickers as post-filters on the merged FTS+semantic hits. State is in the URL so you can bookmark filtered queries.

## What landed in v0.11 (private vault + LLM note autocomplete)

- 🔒 **Private vault** — Click "Make private" on any screenshot, give it a passphrase, and the OCR text + thumbnail get encrypted with AES-256-GCM (PBKDF2 600k). Plaintext is deleted from disk and DB. Viewing requires re-entering the passphrase; decryption happens in-memory and never writes back unless you explicitly Restore. List of vaulted items at `/vault`.
- 🤖 **LLM note draft** — Tiny `📝 Draft note` action on screenshot detail (BYO LLM) writes a 1-2 sentence journal note from the OCR + window title. Russian or English depending on what's on screen.
- 🧪 Vault encrypt/decrypt/restore/wrong-pass tests using cryptography lib (skipped if unavailable).

## What landed in v0.10 (focus + reminders + reading list)

- 🍅 **Focus mode** (`/focus`) — Pomodoro-style timer with 15/25/50/90 min presets, intent ("what I'm focusing on") and outcome fields, today's completed count. Auto-pauses screen capture during the block, resumes when you finish. Sessions are logged.
- 📝 **Reminders** (`/reminders`) — short todos pinned to a single day, with an "overdue" panel showing pending items from earlier days. Not your task manager — just things you don't want to forget today.
- 📚 **Reading list** (`/reading`) — `📚 Read later` button on every screenshot detail page; reading-list view with optional "include read" toggle, marks items read when you click in.
- 🧭 Nav re-shuffled to surface these three; Topics, Time-sheet, Tabs etc. are still one Cmd+K away.

## What landed in v0.9 (mobile + extensions + webhooks)

- 📱 **`/m` mobile companion** — text-only stripped page tuned for phones: search box, recent notes, yesterday's digest excerpt, today's 30 captures. Same backend; just lighter.
- 🪝 **Outbound webhooks** — subscribe HTTPS endpoints to events (`capture.saved`, `digest.daily_generated`, etc.). Optional HMAC-SHA256 signing via the `X-Persona-Signature` header. Manage at `/webhooks`.
- 🌐 **Browser-extension scaffold** — MV3 extension in `browser-extension/` that posts the URL+title of your focused tab once a minute. View ingested tabs at `/companion/tabs` with top-domains chart. Strict CORS — only chrome-extension://* and moz-extension://* origins allowed.
- 🧪 Tests cover companion ingest validation, webhooks CRUD, mobile page rendering.

## What landed in v0.8 (analytics + ergonomics)

- ⏱️ **Per-app time-sheet** — `/timesheet?date=YYYY-MM-DD` shows how many minutes you really spent in each app today. Counts consecutive same-app captures within 5-min gaps; isolated frames get the tick interval. Click an app to drill into its `/apps/{name}` page.
- 🌳 **1-year contributions heatmap** — GitHub-style grid on Stats, click any day-cell to open that day's timeline.
- ⌨️ **Cmd+K / Ctrl+K command palette** — fuzzy-search across all routes, ↑↓ to navigate, Enter to go, Esc to close. Works everywhere in the app.
- 🔄 **`scripts/rebuild_embeddings.py`** — drop & re-index after model change.

## What landed in v0.7 (polish + shareable)

- 🔥 **Streak counter** — current consecutive days, longest streak, 30-day-activity ratio, total active days. Visible in Stats.
- 🌞 **Light mode actually works** — `html:not(.dark)` CSS overrides remap the dark `ink-` palette to off-white so existing templates look passable in light mode. Toggle via ☀/☾ in header (already shipped in v0.5, just no point earlier).
- 📰 **RSS feed of journal** — `/feeds/journal.rss` (RSS 2.0, valid, CDATA-escaped). Subscribe to your own past in any reader.
- 🔗 **Time-limited signed share links** — POST `/api/screenshots/{id}/share` returns `/share/{token}` + thumbnail URL. Token is HMAC-SHA256 over `id|expires|purpose` keyed with `data/.share_secret`. 24-hour TTL by default. Useful when you tunnel Persona temporarily and want to send a single screenshot to someone.
- 🔍 **Apps fuzzy filter** — instant Alpine filter on the Apps index.
- 🧪 Three new test modules: streak math, share-link sign/verify, RSS smoke test.

## What landed in v0.6 (discovery + automation)

- 🖼️ **App icons** — Persona pulls the .exe icon for each running app (Windows-only, via ctypes / shell32) and caches it as 32×32 PNG in `data/icons/`. Now the Apps page shows real icons; `/icons/{name}.png` is the serving route.
- 🧠 **Topic discovery** — `/topics` page runs pure-Python k-means over your stored embeddings to surface "themes" in your recent captures. Each cluster gets an auto-label from frequent OCR tokens (RU+EN stopword-aware). Tunable `k` slider (2-24). Requires `PERSONA_EMBEDDINGS_ENABLED=true`.
- 🌙 **Auto daily digest** — set `PERSONA_AUTO_DIGEST_ENABLED=true` + `PERSONA_AUTO_DIGEST_HOUR_LOCAL=22` (default 22:00 local) and Persona generates your day-end LLM summary itself. Stored in `daily_digest` table, indexed at `/digest/daily`, individual at `/digest/daily/{YYYY-MM-DD}`.
- 🧭 Nav cleaned up: **Topics** + **Apps** promoted, Tags / Help moved out (still reachable directly).

## What landed in v0.5 (visibility + multi-monitor + export)

- 🖥️ **Multi-monitor capture** — flip `PERSONA_MULTI_MONITOR=true` and every connected display gets its own capture record. Dedup runs per-monitor (so each display has its own pHash space).
- 📂 **Per-app pages** — `/apps` lists every app you've used, sorted by capture count. `/apps/{name}` drills in: 30-day activity chart, top window titles, latest 24 captures.
- 📅 **Weekly digest** — `/digest/weekly` shows the past 7 days at a glance: captures total, days active, busiest day, top apps, every note you wrote. Walk backwards with `?weeks_ago=N`.
- ☀ **Theme toggle** in the header (sun/moon icon, localStorage-persisted, no flash on first paint).
- 📦 **`/api/export/full.zip`** — single-click migration archive: full DB snapshot + all thumbnails + manifest.
- 🔴 **Live timeline** — header polls `/api/timeline/new-count` every 15s; a clickable "N new captures" chip appears so you don't need to F5.
- Navigation cleaned up: Apps + Digest promoted into the nav bar, Summary moved into Settings → AI (still reachable at `/summary/`).

## What landed in v0.4 (privacy + Q&A)

- 🔐 **Passphrase-encrypted backup** — `scripts/encrypted_backup.py` produces a single `.pbkx` file: DB + manifest + last-N-days thumbnails, encrypted with AES-256-GCM (PBKDF2-HMAC-SHA256, 600k iterations). Format is documented in `app/backup/crypto.py` — anyone can write a decryptor. Restore via `--restore path.pbkx --restore-dir out/`.
- 🧊 **Auto-archive** to satellite SQLite — once a screenshot has been `cold` for `archive_after_days` (default 180), its row + OCR move to `data/persona_archive.db` (with its own FTS5 index). The live DB stays tiny forever; archive is opt-in via `PERSONA_ARCHIVE_ENABLED=true`.
- 🤔 **Q&A over your memory** — new `/ask` page. Ask in plain Russian/English: "когда я последний раз обсуждал auth?" The retriever runs semantic + FTS in parallel, top-K screenshots become context, your BYO LLM answers with `[#id]` citations to specific captures. No hallucinations allowed — system prompt forbids it.
- 📁 New `app/backup/` package + `app/storage/archive.py` module.
- 🌐 New endpoints: `/api/ask`, `/api/archive/status`, `/api/archive/run`.

## What landed in v0.3 (size-budget release — your goal: 2-4 MB/day)

- 🎯 **Smart-thumbnail capture** — thumbnails saved only when (a) same app hasn't had a thumb for `smart_min_gap_seconds` (default 180s) AND (b) you're under today's MB budget. Otherwise we keep only metadata + OCR + embedding (~1KB total). At default settings this brings a typical 8h workday to **2-4 MB**.
- 🌡️ **Tiered storage** — `hot` (full thumb, recent) → `warm` (320px q=30, 8-30d) → `cold` (metadata only, 30+d). Re-compression saves ~70% of historic disk. Old metadata + OCR stays forever for search.
- 📌 **Pin** — mark a screenshot pinned and it's never demoted. Perfect for "this moment matters". Visible on detail page + small 📌 badge on timeline.
- 📊 **Live budget badge in header** — green / amber / red MB count, links to Stats.
- 📈 **Stats: size-budget card** — today's MB vs target + 14-day bar chart (bars turn red on over-budget days) + 4-tier counter card.
- 🔧 **`scripts/recompress_tiers.py`** — manually rebalance after changing tier settings.
- ⚙️ **Tighter defaults** — thumbnail q=45 (was 60), max width 900 (was 1280), retention 180d (was 30). All overridable via `.env`.

## What landed in v0.2 (the flagship features)

- 🧠 **Semantic search via local ONNX embeddings** — flagship feature. `fastembed` + `intfloat/multilingual-e5-small` (~120MB, downloaded on first use). Ask "what was that auth bug I was debugging?" — even if those exact words never appeared on screen.
- 🎯 **Hybrid ranking** — keyword (FTS5) + semantic results merged into one list. Mode toggle in the search UI.
- 📓 **Journal view** (`/journal`) — every screenshot you wrote a note on, grouped by day, markdown-rendered.
- ✍️ **Markdown notes** with Write/Preview toggle (markdown-it via CDN).
- 🆘 **Help page** (`/help`) — shortcuts, search-mode guide, privacy reminders, CLI cheatsheet.
- 🚨 **Bulk delete** by app or by date range (Settings → Danger zone).
- 🪟 **Top windows** alongside Top apps in Stats.
- 📊 **Live indexing badges** in header — see how many screenshots still need OCR / embeddings.
- 🧹 **Orphan-thumbnail cleanup** script.
- Better empty states throughout.

## How to run

```powershell
cd C:\www-Yaroslav\Persona
uv sync
copy .env.example .env
uv run python scripts/setup_database.py
uv run python scripts/check_environment.py
uv run uvicorn app.web.main:app --host 127.0.0.1 --port 8765
```

Empty DB redirects to `/welcome`. Click **Start** in the header.

### Enable semantic search (recommended)

```powershell
uv sync --extra embeddings
```

Then in `.env`:

```
PERSONA_EMBEDDINGS_ENABLED=true
```

Restart. fastembed downloads `multilingual-e5-small` (~120MB) on first use into `data/models/`. The embeddings worker indexes existing OCR'd screenshots automatically.

### Enable OCR

```
PERSONA_TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
PERSONA_OCR_ENABLED=true
```

After installing Tesseract from <https://github.com/UB-Mannheim/tesseract/wiki>.

### Enable AI daily summary

```
PERSONA_BYO_API_PROVIDER=anthropic   # or openai, groq
PERSONA_BYO_API_KEY=sk-ant-...
```

Then visit `/summary/`. Requests go directly to the provider — Persona never sees your key in the cloud.

## Routes (22)

| Route | Purpose |
|---|---|
| `/` | Timeline (redirects to /welcome on empty DB) |
| `/calendar` | Month-at-a-glance |
| `/search` | Keyword + semantic search with mode toggle |
| `/journal` | Notes-only diary, markdown-rendered |
| `/tags`, `/tags/{id}` | Tag list + detail |
| `/diff?left=X&right=Y` | OCR token diff + pHash hamming |
| `/sessions?date=...` | Focus-session clustering |
| `/screenshot/{id}` | Detail with OCR, note (markdown), tags, diff-vs-previous |
| `/summary/` | BYO LLM daily summary |
| `/stats` + `/stats.json` | Top apps / top windows / heatmap / OCR breakdown |
| `/settings` | Config + Tesseract probe + Danger-zone bulk delete |
| `/whitelist` | Process allow/deny lists |
| `/welcome` | First-run onboarding |
| `/help` | Keyboard shortcuts + tips |
| `/health` | Liveness probe |
| `/api/capture/{start,pause,status,now}` | Capture control + manual single shot |
| `/api/ocr/status` | OCR pipeline progress |
| `/api/embeddings/status` | Embeddings pipeline progress |
| `/api/export/{day,range,search.csv,search.md}` | Exports |
| `/api/tags`, `/api/saved-searches` | Manipulation |
| `/api/screenshots/{id}/{tags,note}` | Per-shot manipulation |
| `/api/bulk/{delete-by-app,delete-by-range}` | Bulk deletes |
| `/thumbs/...` | Serve WebP thumbnails |

## Recommended next steps for you

1. Run the quickstart. Open the UI. Click around.
2. `uv run pytest -q` to see test suite status (some Windows-only tests may skip elsewhere).
3. `uv run ruff format --check . && uv run ruff check . && uv run mypy app`
4. Try semantic search after enabling — ask vague questions, see if it finds things FTS missed.
5. Live with it for a few days. Take notes in the Journal. That feedback is the input for v0.3.

## Backlog ideas (not implemented)

- Multi-monitor support
- Cloud sync (BYO R2/B2 bucket → encrypted blobs)
- Zero-knowledge encryption (Age/AES-256 + passphrase-derived key)
- macOS capture via ScreenCaptureKit
- Mobile note-only client
- Server-Sent Events for live timeline updates
- LLM-powered Q&A over your captures (not just summaries)
- Auto-archive: roll old screenshots into a separate compressed DB after N months
- Reproducible builds + code-signed installer

— Claude
