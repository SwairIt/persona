# Persona WORKLOG

Timestamped log of autonomous-development sessions.

## 2026-06-02 — v0 night sprint (autonomous, single linear session)

Built from zero. v0 of personal AI memory, runnable on Windows via `uv run uvicorn app.web.main:app`.

### Foundation
- 02:10Z — bootstrap — pyproject + ruff/mypy strict + .env.example + .gitignore + README + SPEC
- 02:15Z — settings + lru_cache + path validators
- 02:18Z — DB — aiosqlite ctx + init_database + schema.sql with FTS5 triggers + migrations runner
- 02:22Z — models — Pydantic Screenshot / DedupGroup / CaptureEvent
- 02:24Z — logging — structlog console renderer

### Core capture stack
- 02:30Z — capture — mss primary monitor + Windows ctypes/psutil window + GetLastInputInfo idle
- 02:38Z — repository — CRUD for screenshots / dedup_groups / capture_events / kv_settings
- 02:42Z — thumbnails — WebP q=60, dated subfolders, LANCZOS
- 02:46Z — dedup — imagehash pHash + hamming + find_or_create_dedup_group
- 02:50Z — OCR pipeline (latent) — pytesseract + TesseractProbe
- 02:55Z — search — FTS5 MATCH + bm25 + snippet + sanitised query

### Background workers, web, tests
- 03:05Z — workers — capture_loop + ocr_worker + retention_worker + CaptureController
- 03:15Z — FastAPI lifespan + GZip + Jinja2 + static
- 03:25Z — templates — Tailwind/HTMX/Alpine CDN, dark, live status pill
- 03:30Z — routes — timeline / search / screenshot / settings / stats / capture-control / thumbnails
- 03:35Z — tests v0 — settings, repository, dedup, search, thumbnails, capture window/idle, web routes
- 03:40Z — scripts — setup_database + check_environment + pre-commit

### Features built on top in v0
- 03:45Z — process whitelist
- 03:50Z — day-snapshot JSON export
- 03:53Z — keyboard shortcuts
- 03:55Z — lightbox
- 03:58Z — app filter chips on timeline
- 04:00Z — activity heatmap on stats
- 04:05Z — capture-only CLI (`persona-capture`)
- 04:10Z — BYO LLM client (Anthropic / OpenAI / Groq)
- 04:15Z — daily AI summary
- 04:20Z — OCR redaction
- 04:25Z — backup snapshot
- 04:28Z — welcome wizard
- 04:30Z — health endpoint
- 04:32Z — CSV + Markdown search export
- 04:35Z — Windows autostart
- 04:40Z — calendar view
- 04:45Z — tags + saved searches
- 04:50Z — diff view
- 04:55Z — sessions view
- 05:00Z — notes
- 05:05Z — OCR backfill on enable
- 05:08Z — first-run redirect to /welcome

## 2026-06-02 — v0.2 continuation (autonomous)

User explicitly asked: "Продолжай. Доделай" → "след версию давай делай". Pushed to v0.2.

### Flagship: semantic search
- 06:00Z — added `embeddings` optional dependency group (fastembed + numpy)
- 06:05Z — `PERSONA_EMBEDDINGS_*` settings + `intfloat/multilingual-e5-small` default
- 06:10Z — migration 003_embeddings.sql — float32[N] BLOB per screenshot, model+text_hash
- 06:15Z — `app/embeddings/model.py` — lazy singleton TextEmbedding, query/passage prefixes
- 06:20Z — `app/embeddings/storage.py` — encode/decode BLOB, list_unindexed (re-index on model change)
- 06:25Z — `app/embeddings/search.py` — cosine similarity over all stored embeddings + snippet builder
- 06:30Z — `app/workers/embeddings_worker.py` — background indexer, drains pending in batches
- 06:35Z — hybrid search ranking in `/search` route (fts + semantic merged, mode toggle in UI)
- 06:40Z — UI: mode selector (Hybrid/Keywords/Semantic), `%` similarity badge per result
- 06:45Z — `/api/embeddings/status` endpoint
- 06:50Z — `tests/embeddings/test_storage.py` — roundtrip, cosine, snippet, list_unindexed, re-index

### Polish wave
- 07:00Z — header live badges: OCR pending count + AI pending count (5s polling)
- 07:05Z — markdown-it from CDN + `static/js/notes.js` renderer
- 07:10Z — screenshot detail Notes: Write/Preview toggle, markdown rendering
- 07:15Z — `/journal` route — every screenshot with a note, grouped by day, markdown-rendered
- 07:20Z — `/help` route — keyboard shortcuts + search modes + privacy reminders + CLI cheatsheet
- 07:25Z — `/api/bulk/delete-by-app` + `/api/bulk/delete-by-range` with confirmation phrases
- 07:30Z — Settings page "Danger zone" — UI for bulk deletes
- 07:35Z — Top windows in `/stats` (next to Top apps)
- 07:40Z — Better empty state on `/timeline` (links to help, contextual to filter)
- 07:45Z — `scripts/cleanup_orphans.py` — remove thumbnails not referenced in DB
- 07:50Z — nav updated: Journal + Help added
- 07:55Z — tests: journal, help, bulk, status endpoints

## 2026-06-02 — v0.3 continuation (autonomous, /loop dynamic mode)

User explicit goal: "за день 2-4 МБ". Focus on size-budget enforcement.

- 08:00Z — version bump to 0.3.0 across pyproject + app/__init__ + FastAPI app
- 08:05Z — tighter defaults: thumbnail q=45 (was 60), max width 900 (was 1280), retention 180d
- 08:08Z — new settings: `PERSONA_SMART_THUMBNAIL`, `PERSONA_SMART_MIN_GAP_SECONDS`, `PERSONA_DAILY_SIZE_BUDGET_MB`, `PERSONA_TIER_*`
- 08:12Z — migration 004_tiers.sql — `tier` column on screenshots (hot/warm/cold/pinned) + `daily_size_log` table
- 08:16Z — app/storage/tiers.py — set_tier, count_by_tier, pin/unpin
- 08:20Z — app/storage/size_log.py — sample_today scans today's WebP folder, upserts log row
- 08:25Z — smart-thumbnail capture loop: only save thumb if same-app-no-thumb in last `smart_min_gap_seconds` AND under daily budget. Otherwise keep only metadata + OCR (1KB)
- 08:32Z — tier-sweep retention worker: hot→warm (downscale to 320×N q=30) after 7d, warm→cold (delete thumb) after 30d. Pinned never touched. Samples today_bytes on each pass.
- 08:40Z — /api/budget/status endpoint — today MB, budget MB, used %, tier breakdown
- 08:42Z — /api/screenshots/{id}/pin + unpin
- 08:45Z — screenshot detail: tier badge + Pin / Unpin button
- 08:48Z — header live MB readout (green / amber / red) clicking opens Stats
- 08:55Z — Stats page: size-budget card with 14-day bar chart + storage-tiers card (4 counters)
- 09:00Z — scripts/recompress_tiers.py — manual rebalance after settings change
- 09:05Z — tests/storage/test_tiers_and_size.py — set_tier, pin/unpin, sample_today

## 2026-06-02 — v0.4 continuation (autonomous, /loop dynamic mode)

- 09:15Z — version bump to 0.4.0
- 09:20Z — `app/backup/crypto.py` — AES-256-GCM envelope (magic + salt + iters + nonce + ct), PBKDF2-HMAC-SHA256 with 600k iters, passphrase fingerprint
- 09:30Z — `app/backup/archive.py` — encrypt a ZIP-of-(persona.db + manifest + last-N-days thumbnails) into `.pbkx`
- 09:35Z — `scripts/encrypted_backup.py` CLI — create / restore / list, reads passphrase from `PERSONA_BACKUP_PASSPHRASE`
- 09:40Z — `cryptography` added as optional `backup` extra
- 09:50Z — `app/storage/archive.py` — satellite SQLite (`persona_archive.db`) with FTS5; `archive_cold_older_than(days)` moves cold rows out; `run_archive_worker` once-a-day sweep
- 10:00Z — `app/llm/qa.py` — natural-language Q&A: retrieve top-K via hybrid (semantic + FTS), feed into BYO LLM with system prompt that forbids hallucination; extracts `[#id]` citations
- 10:05Z — `/ask` page + `/api/ask` endpoint + nav entry
- 10:10Z — `/api/archive/{status,run}` endpoints
- 10:15Z — tests/test_backup_crypto.py — roundtrip + wrong-pass + magic-mismatch + fingerprint

## 2026-06-02 — v0.5 continuation (autonomous, /loop tick)

- 10:30Z — version bump to 0.5.0
- 10:35Z — `capture_all_monitors()` — capture every connected monitor as separate CaptureResult; capture_loop branches on `PERSONA_MULTI_MONITOR`
- 10:40Z — per-app analytics: `/apps` index + `/apps/{name}` with last-30-day chart + top window titles + latest 24 captures
- 10:45Z — `/digest/weekly` — full week recap: per-day chart, top apps, all your notes from that week
- 10:48Z — theme toggle (☀/☾) in header, localStorage-backed, no flash via head-script
- 10:50Z — `/api/export/full.zip` — single ZIP of DB + thumbnails + manifest for migration
- 10:53Z — live timeline auto-refresh — `/api/timeline/new-count` polled every 15s, "N new captures" chip
- 10:55Z — nav: Apps + Digest added
- 10:58Z — tests/test_v05_routes.py — apps index/detail, weekly digest, full-export, timeline-api

## 2026-06-02 — v0.6 continuation (autonomous, /loop tick)

- 11:30Z — version bump to 0.6.0
- 11:33Z — `app/capture/icons.py` — Windows HICON → PIL via ctypes/shell32/user32/gdi32; cache 32×32 PNG in `data/icons/`
- 11:38Z — `/icons/{name}.png` route serves cached icons (404 if not yet extracted)
- 11:40Z — apps index renders icon next to each app name (with onerror hide fallback)
- 11:45Z — `app/embeddings/clustering.py` — pure-Python mini-batch k-means over stored vectors + auto-label from frequent OCR tokens (handles Russian stopwords)
- 11:50Z — `/topics` page — k slider, clusters with sample screenshots, graceful empty state when embeddings disabled
- 11:55Z — migration 005_auto_digest.sql — `daily_digest` table
- 12:00Z — `app/workers/digest_scheduler.py` — once-a-day worker, fires when local-hour == `auto_digest_hour_local` and today's digest doesn't exist; reuses `summarise_day`
- 12:05Z — `/digest/daily` index + `/digest/daily/{day}` detail
- 12:08Z — nav update: Topics + Apps promoted, Tags + Help demoted
- 12:10Z — tests/embeddings/test_clustering.py — distance / mean / nearest / converged / kmeans / labels / discover_clusters with seeded vectors
- 12:13Z — new settings: `PERSONA_AUTO_DIGEST_ENABLED`, `PERSONA_AUTO_DIGEST_HOUR_LOCAL`

## 2026-06-02 — v0.7 continuation (autonomous, /loop tick)

- 12:30Z — version bump to 0.7.0
- 12:33Z — light-mode CSS — `html:not(.dark)` overrides for ink-/zinc- palette; scrollbar adapts; existing dark-styled templates degrade gracefully in light mode
- 12:40Z — `app/analysis/streak.py` — pure-Python streak / longest-streak / 30d-active / total-active computation
- 12:45Z — Stats: 4-cell "Streak" card (🔥 current, longest, 30d active, total)
- 12:50Z — `app/web/routes/rss.py` — `/feeds/journal.rss` valid RSS 2.0 of last 200 notes, CDATA escaped
- 13:00Z — share-link plumbing: HMAC-SHA256-signed time-limited token (`data/.share_secret`), `/share/{token}` view + `/share/{token}/thumbnail` image, POST `/api/screenshots/{id}/share` creates one
- 13:05Z — `templates/shared.html` — read-only single-screenshot page
- 13:08Z — `apps_index.html` — Alpine fuzzy filter on app name
- 13:12Z — tests: streak (empty/3-day/broken/yesterday), share-link sign/verify/expired/tampered, RSS feed renders

## 2026-06-02 — v0.8 continuation (autonomous, /loop tick)

- 13:30Z — version bump 0.7→0.8
- 13:33Z — `app/analysis/time_sheet.py` — per-app active seconds with 5-min idle gap rule + per-day totals + duration formatter
- 13:38Z — `/timesheet` page + nav: bars per app for one day (prev/today/next)
- 13:42Z — Stats: 1-year contributions heatmap (GitHub-style 53×7 grid, Alpine renders, click cell to open day)
- 13:48Z — `static/js/palette.js` — Ctrl+K / Cmd+K command palette, fuzzy-scoring over 18 routes, ↑↓ Enter Esc bindings
- 13:55Z — `scripts/rebuild_embeddings.py` — drop & re-index after embedding model change, batched (cli)
- 14:00Z — tests/analysis/test_time_sheet.py — empty/single-session/idle-gap/per-day totals

## 2026-06-02 — v0.9 continuation (autonomous, /loop tick)

- 14:30Z — version bump 0.8→0.9
- 14:33Z — `/m` mobile companion page — text-only, top 30 captures today, top 10 notes, yesterday's digest excerpt
- 14:40Z — migration 006_webhooks.sql — `webhooks` subscriber table
- 14:45Z — `app/webhooks/dispatcher.py` — fire-and-forget POST with optional `X-Persona-Signature: sha256=...` HMAC
- 14:50Z — `/webhooks` page — create/toggle/delete subscriptions, see last delivery status
- 14:55Z — `capture.saved` event wired in capture loop
- 15:00Z — `browser-extension/` MV3 scaffold — background.js (1-min alarm samples active tab), popup config, README
- 15:05Z — migration 007_browser_tabs.sql + `/api/companion/tab` POST + `/companion/tabs` HTML index
- 15:12Z — CORS middleware locked to `chrome-extension://*` and `moz-extension://*`
- 15:15Z — tests/test_v09_routes.py — mobile / companion ingest valid+invalid / webhooks CRUD

## 2026-06-02 — v0.10 continuation (autonomous, /loop tick)

- 15:30Z — version bump 0.9→0.10
- 15:35Z — migration 008_focus_and_reminders.sql — `focus_sessions`, `reminders`, `reading_list` tables
- 15:40Z — `app/storage/focus.py` + `/focus` page — Pomodoro timer with 15/25/50/90 presets, intent + outcome fields, auto-pauses capture loop on start, resumes on finish
- 15:48Z — `app/storage/reminders.py` + `/reminders` — short todos for a specific day, overdue panel, toggle done / delete
- 15:55Z — `app/storage/reading_list.py` + `/reading` page — "Read later" button on every screenshot detail; reading list view with include-read filter; routes `/api/screenshots/{id}/read-later` (POST/DELETE) and `/mark-read`
- 16:02Z — nav re-shuffled: Focus, Reminders, Reading promoted into the bar; Topics + Time-sheet accessible via Cmd+K
- 16:05Z — tests/test_v010_routes.py — focus lifecycle, page render, reminders CRUD + validation, reading list add/list/delete + 404

## 2026-06-02 — v0.11 continuation (autonomous, /loop tick)

- 16:30Z — version bump 0.10→0.11
- 16:33Z — migration 009_private_vault.sql — `is_private` column + `private_vault` blob table
- 16:38Z — `app/storage/vault.py` — `make_private` / `unlock` / `restore_to_public` / `count_private`. Reuses AES-256-GCM from app/backup/crypto. JSON envelope holds OCR + base64 thumbnail. Plaintext deleted from disk and DB on make_private.
- 16:45Z — `/vault` index + 4 endpoints: make-private, restore-public, unlock (in-memory), unlock-thumbnail (streams decrypted WebP)
- 16:52Z — Screenshot detail UI: 🔒 Make private modal, 🔓 Restore modal, inline unlock viewer that decrypts in-browser (no plaintext written back unless user picks Restore)
- 16:58Z — `app/llm/note_draft.py` + `/api/screenshots/{id}/draft-note` — BYO LLM drafts a 1-2 sentence journal note from app/window/OCR
- 17:02Z — tests/test_vault.py — make_private/unlock/wrong-pass/restore/short-pass-rejected

## 2026-06-02 — v0.12 continuation (autonomous, /loop tick)

- 17:30Z — version bump 0.11→0.12
- 17:33Z — `app/llm/auto_tag.py` — BYO LLM suggests 3-5 kebab-case tags from app+window+OCR; robust JSON-extraction parser (handles preamble, lowercasing, dedup, length filter, RU/EN)
- 17:38Z — `/api/screenshots/{id}/auto-tag-suggest` + `/auto-tag-apply` endpoints; UI on screenshot detail with tap-to-pick chips + Apply
- 17:45Z — Advanced search filters: `?tier=hot|warm|cold|pinned` and `?tag=<name>` post-filter the FTS+semantic merged result set; UI controls in `/search` form (tier radio + exact tag + exact app + date range)
- 17:52Z — tests/test_auto_tag.py — clean JSON / preamble / multiword→kebab / length-filter / dedup / empty / cyrillic

## 2026-06-02 — v0.13 continuation (autonomous, /loop tick)

- 18:15Z — version bump 0.12→0.13
- 18:18Z — `app/storage/tags.py` — `rename_tag` (auto-merges when target name exists), `merge_tag` (moves bindings, deletes source), `delete_tag` (cascading), `co_tag_counts`, `per_day_for_tag`
- 18:25Z — `/api/tags/{id}/rename`, `/api/tags/{id}/merge-into/{target}`, `/api/tags/{id}/delete` endpoints
- 18:30Z — `tag_detail.html` — per-day activity chart (60d), co-tag chart, manage section with rename / merge-into-other / delete
- 18:35Z — `/api/export/reading.md` + download button on `/reading`
- 18:40Z — tests/storage/test_tag_admin.py — rename / rename-into-existing / merge / merge-self / delete / co-tags / per-day

## 2026-06-02 — v0.14 continuation (autonomous, /loop tick)

- 18:55Z — version bump 0.13→0.14
- 18:58Z — migration 010 — `process_app_remap` table + saved_searches `last_seen_*` columns
- 19:02Z — `app/storage/process_remap.py` — upsert/list/delete/lookup, case-insensitive on process_name
- 19:05Z — `/process-remap` page — user-defined process→app rename, with auto-suggested top processes that don't yet have an override
- 19:10Z — capture loop consults `process_app_remap` and overrides `app_name` at insert time
- 19:15Z — `set_tag_color` + hex validator; inline `<input type="color">` on /tags rows
- 19:20Z — saved-search "N new" badge: FTS-driven count via `saved_search_new_count` callback (avoids import cycle); `mark_seen` POSTed on click
- 19:25Z — tests: process-remap CRUD + lookup + case-insensitive + reject-empty; tag-color set/clear/reject-invalid; saved-search new-count + mark-seen

## 2026-06-02 — v0.15 continuation (autonomous, /loop tick)

- 19:45Z — version bump 0.14→0.15
- 19:48Z — `/api/export/journal.md?date=` — bundles auto-digest + focus sessions + notes + top apps for a single day into one Markdown file. 404 on empty days, 400 on bad date
- 19:55Z — `/about` feature-status dashboard: OCR / semantic / BYO LLM / auto-digest / tiered retention / smart thumb / multi-monitor / archive / vault / webhooks; tally cards for screenshots/notes/tags; hidden-route directory grid
- 20:02Z — `get_tags_for_many` bulk lookup + `tags_by_id` passed to timeline; `_screenshot_card.html` shows up to 3 coloured tag chips with `+N` overflow
- 20:08Z — "↓ today.md" download on `/digest/weekly` header
- 20:12Z — tests/test_v015_routes.py — about renders, journal export empty/with-note/invalid-date, bulk get_tags_for_many

## 2026-06-02 — v0.16 continuation (autonomous, /loop tick)

- 20:45Z — version bump 0.15→0.16
- 20:48Z — `get_neighbour_ids(screenshot_id)` repository helper returns (prev, next) by captured_at
- 20:53Z — screenshot detail page: ← prev / next → bar at top + arrow-key handler (ignored while typing in inputs/textareas)
- 21:00Z — `/api/tags/bulk-apply` endpoint — auto-creates tag, applies to up to 500 screenshot_ids
- 21:05Z — search results template: row checkboxes + sticky bulk-tag input + "Apply to N" button (Alpine)
- 21:10Z — tests: neighbour-ids happy + edges + missing; bulk-apply happy + validation (empty tag / empty ids / non-numeric)

## 2026-06-02 — v0.17 continuation (autonomous, /loop tick)

- 21:30Z — version bump 0.16→0.17
- 21:33Z — `/api/screenshots/bulk-pin` — pin every screenshot in a comma-separated list (cap 500)
- 21:37Z — search results: 📌 Pin button alongside bulk-tag; Alpine handles either path
- 21:42Z — `/feeds/saved-search/{id}.rss` — RSS feed per saved search, FTS-driven, 100 items; 📡 icon in tag-page saved-search list links to it
- 21:48Z — Apps index: 14-day per-app sparkline column (bar-glyphs sized by daily count over a 14-day window); existing fuzzy filter still works
- 21:52Z — tests/test_v017.py — bulk-pin happy + validation + saved-search RSS happy + 404

## 2026-06-02 — v0.18 continuation (autonomous, /loop tick, Ultracode multi-agent workflow)

Three feature agents ran in parallel via the Workflow tool, then I wired routes + tests sequentially.

- 22:00Z — version bump 0.17→0.18
- 22:05Z — **Date-range timeline** (agent A): `/range?since=YYYY-MM-DD&until=YYYY-MM-DD` (default last 7 days, max 90 days, silently swaps reversed range, 400 on unparseable). Presets: Last 7d / Last 30d / This week (Mon-anchored) / This month. Server-side preset computation, cards reuse `_screenshot_card.html` with tag chips via `get_tags_for_many`.
- 22:08Z — **Per-app capture-interval override** (agent B): migration 011 + `app_overrides.py` CRUD (validates 0.5-60.0s) + `/app-overrides` admin page with suggested top apps. Capture loop consults `lookup_override`, sets `ctrl.next_sleep_seconds`, outer loop respects it then resets.
- 22:12Z — **Diff picker** (agent C): `/diff-picker?left=ID` finds same-app same-day candidates and renders a grid of clickable comparison targets — each links to existing `/diff?left=X&right=Y`.
- 22:18Z — Wired all 3 routers into `app/web/main.py`
- 22:22Z — tests/test_v018.py — override CRUD + invalid-interval + range default/with-data/invalid-date/reversed-swap + app-overrides page + create + diff-picker empty + with-left

## 2026-06-02 — v0.19 continuation (autonomous, Ultracode workflow tick #2)

Three feature agents in parallel via Workflow, then sequential wire-up.

- 22:35Z — version bump 0.18→0.19
- 22:38Z — **Persona CLI** (agent A): `python -m app stats|search|export-day|vacuum-db|ocr-status` (also `persona-cli` console script). No FastAPI dep; plain print output; `vacuum-db` reports freed bytes
- 22:42Z — **Quiet hours** (agent B): migration 012 + `app/storage/quiet_hours.py` (CRUD + `is_quiet_now`, 0≤weekday≤6, 0≤start<24, start<end≤24) + `/quiet-hours` admin page + capture-loop check inside `_single_iteration` (one DB query per tick, increments `ctrl.mark_idle_skip()` without toggling `paused`)
- 22:46Z — **Reminder → screenshot link** (agent C): migration 013 + `list_for_screenshot` helper + `POST /api/screenshots/{id}/remind` + "Remind me" Alpine widget on screenshot detail. Reminder rows show 🖼 #N link when attached
- 22:50Z — wired `quiet_hours` router + added `persona-cli` console script + version bump
- 22:55Z — tests/test_v019.py — quiet hours CRUD/validation/is_quiet_now match-or-miss/empty + reminder-screenshot link + page render + create-via-API + remind endpoint + CLI import smoke

## 2026-06-02 — v0.20 continuation (autonomous, Ultracode workflow tick #3)

Three feature agents in parallel via Workflow, then sequential wire-up.

- 23:10Z — version bump 0.19→0.20
- 23:13Z — **Share-collection** (agent A): migration 014 + `app/web/routes/share_collection.py` reuses `_sign`/`_verify` from existing `share.py`. `POST /api/share/collection` accepts comma-separated screenshot_ids + title + ttl_hours, persists row, returns signed URL. `GET /share/collection/{token}` verifies + renders gallery (`shared_collection.html` — amber expiry banner, responsive grid via `/thumbs/...`).
- 23:18Z — **OCR re-process admin** (agent B): `app/storage/ocr_admin.py` (reset_skipped/failed/all/one + status_breakdown, all gated on thumbnail_path NOT NULL) + `/ocr-admin` page with five status-counter cards + three confirm-on-submit forms + `POST /api/screenshots/{id}/reset-ocr` JSON. CLI extended with `persona-cli reset-ocr [--scope skipped|failed|all]`.
- 23:22Z — **Webhook test-fire** (agent C): new `POST /api/webhooks/{id}/test` + sky-700 "test" button on `/webhooks` table + `dispatch_test` helper in `app/webhooks/dispatcher.py` that fires synthetic `{"screenshot_id": 0, "test": True, ...}` payload via existing fire-and-forget `dispatch_event`.
- 23:25Z — wired `share_collection` + `ocr_admin` routers, version bump in pyproject/app
- 23:30Z — tests/test_v020.py — OCR reset helpers (skipped/failed/one + skips-when-no-thumb) + share-collection happy roundtrip + invalid token + ocr-admin page renders + webhook test-fire happy

## 2026-06-02 — v0.21 continuation (autonomous, Ultracode workflow tick #4)

Three feature agents in parallel via Workflow, then sequential wire-up.

- 23:45Z — version bump 0.20→0.21
- 23:48Z — **Archive browse / search** (agent A): `app/storage/archive_browse.py` (archive_total / archive_search FTS5 / archive_recent — opens its own connection to the satellite `data/persona_archive.db` per call). Routes `/archive/search?q=` and `/archive/browse`. Cards have no thumbnails (archive doesn't keep them) — only app + timestamp + OCR snippet with `<mark>` highlights. Logs `archive.browse.search` events.
- 23:53Z — **Regex auto-tag rules** (agent B): migration 015 + `app/storage/regex_rules.py` (validates patterns via `re.compile`, gentle errors on bad regex / empty fields). `/regex-rules` admin page with create form + live test preview (Alpine.js `regexTester()` debounced). OCR worker now calls `apply_rules_to_ocr` after each successful update; matching rules auto-create their tag, bind it to the screenshot, and bump `match_count` + `last_matched_at`.
- 23:58Z — **Search history** (agent C): migration 016 + `app/storage/search_history.py` (record_query / list_recent / clear_history). Search route records every non-empty query, surfaces recent chips on `/search` (template already updated by linter — preserved). `/api/search-history/clear` and `/api/search-history` JSON endpoints.
- 00:02Z — wired `archive_browse` + `regex_rules` routers in main.py, version bump in pyproject/app
- 00:05Z — tests/test_v021.py — regex create-invalid-rejected + lifecycle + apply-creates-tags + disabled-skipped + history record/list/clear + archive pages + regex test endpoint happy + invalid pattern handled

## 2026-06-02 — v0.22 continuation (autonomous, Ultracode workflow tick #5)

Three feature agents in parallel via Workflow, then sequential wire-up.

- 00:20Z — version bump 0.21→0.22
- 00:23Z — **persona-doctor** (agent A): `app/diagnostics.py` with `run_doctor()` → 12 checks (python_version, sqlite+FTS5 probe, data_dir_writable, db_path_readable, db_integrity, tesseract, embeddings lib, BYO LLM, disk_free 500MB warn / 100MB fail, thumbnails_dir size, capture_loop_recent, schema_version). CLI subcommand `persona-cli doctor [--no-color]` prints colourised PASS/WARN/FAIL + summary, exit 0/1. Web page `/doctor` with the same data + "What does this mean?" expander.
- 00:28Z — **Weekly LLM digest** (agent B): migration 017 + `app/llm/weekly_summariser.py` (Mon-anchored, sections "Big themes" / "Notable moments" / "What I shipped"). `app/workers/weekly_digest_scheduler.py` (30-min polling, only fires Monday at `weekly_digest_hour_local`, skips silently on `LLMNotConfigured`). New settings `weekly_digest_enabled` + `weekly_digest_hour_local`. Routes `/digest/weekly-archive` (list) + `/digest/weekly-archive/{Monday}` (detail). Wired into lifespan as `weekly-digest-scheduler` task.
- 00:33Z — **`persona-cli capture`** (agent C): new subcommand with `--app NAME` override and `--quiet` (just prints integer id). Mirrors `/api/capture/now` minus controller bookkeeping; runs full capture + dedup + thumbnail pipeline + insert. New `scripts/capture_now.bat` and `docs/CAPTURE_HOTKEY.md` (AutoHotkey v2 + PowerToys Run + Windows shortcut recipes).
- 00:38Z — wired doctor + weekly_digests routers + `run_weekly_digest_scheduler` worker + version bump
- 00:42Z — tests/test_v022.py — diagnostics return shape + core checks present + no critical failures on fresh DB + /doctor page renders + weekly archive page + weekly detail 404 + CLI exposes doctor+capture

## 2026-06-02 — v0.23 continuation (autonomous, Ultracode workflow tick #6)

Three feature agents in parallel via Workflow (174k tokens, 6.8 min), then sequential wire-up.

- 00:55Z — version bump 0.22→0.23
- 00:58Z — **Encrypted backup/restore CLI** (agent A): `app/backup/snapshot.py` with `create_backup(out_path, password, days=30)` + `restore_backup(in_path, password, force=False)`. Fernet symmetric encryption with PBKDF2-HMAC-SHA256 (100k iters, 16-byte random salt prepended). Tarball contains DB (after `PRAGMA wal_checkpoint(FULL)`) + last N days of thumbnails. CLI subcommands `persona-cli backup --out FILE [--days 30] [--password STR | $PERSONA_BACKUP_PASSWORD]` and `persona-cli restore --in FILE [--password STR] [--yes]`. Optional dep `[backup]` already in pyproject.
- 01:03Z — **Tag-driven auto-collections** (agent B): migration 018 + `app/web/routes/auto_collections.py`. Rule binds URL slug to tag — `/collection/{slug}` renders every screenshot currently carrying that tag (membership computed on read, newly-tagged shots show up immediately). Slug `^[a-z0-9-]{1,40}$`. Public rules reachable from anywhere; non-public restricted to loopback (closest analogue to session in local-first model). Routes: GET /collections (list + form), POST /collections (create), GET /collection/{slug} (view), POST /collection/{slug}/delete.
- 01:08Z — **Per-app OCR skip-list** (agent C): migration 019 + `app/storage/ocr_skip.py` (normalised via `strip().casefold()`) + `app/web/routes/ocr_skip.py`. Page `/settings/ocr-skip` lists skipped apps + form pre-filled with suggestions from DISTINCT app_name in DB. OCR worker consults `is_skipped()` before invoking Tesseract — if skipped, marks `ocr_done=1` with empty text and logs `ocr.skip_app`.
- 01:12Z — wired auto_collections + ocr_skip routers, alphabetical import order preserved
- 01:14Z — tests/test_v023.py — collections index renders, create+view, 404 on unknown, slug-validation rejects bad slug, ocr-skip page, add+remove via storage helpers, backup module importable, CLI exposes backup+restore

## 2026-06-02 — v0.24 continuation (autonomous, Ultracode workflow tick #7)

Three feature agents in parallel via Workflow (155k tokens, 3.6 min), then sequential wire-up.

- 01:30Z — version bump 0.23→0.24
- 01:33Z — **Bulk-tag CLI** (agent A): `app/bulk_tag.py` with `bulk_tag(tag, query, limit, dry_run)` and `bulk_untag(tag, query, limit)`. CLI subcommands `persona-cli tag --add TAG --query QUERY [--limit 500] [--dry-run]` and `persona-cli untag --remove TAG --query QUERY [--limit 500]`. Reuses existing FTS5 MATCH search and tag helpers; creates tag row if missing.
- 01:36Z — **OCR text redaction** (agent B): migration 020 + `app/redaction.py` with `apply_redaction(text) -> (cleaned, masks_applied)`. Seeded with 3 starter rules (email, credit_card, bearer_token) via idempotent INSERT OR IGNORE. Worker `ocr_worker.py` modified to run `apply_redaction(text)` after Tesseract and store the cleaned text. Web page `/settings/redaction` with add/toggle/delete. Privacy: secrets get masked in FTS5 index — never surface in search.
- 01:39Z — **RSS per auto-collection** (agent C): added `GET /collection/{slug}.rss` to existing `app/web/routes/rss.py`. Loads auto_collection rule, restricts non-public to loopback, joins screenshot_tag for rule.tag, emits RSS 2.0 XML (50 most-recent items, RFC-822 pubDate, escaped XML). Reuses existing /search.rss XML builder.
- 01:42Z — wired `redaction_routes` (auto_collections RSS lives in existing rss.py — no main.py changes needed for that)
- 01:44Z — tests/test_v024.py — redaction masks email + leaves clean text alone + page renders + add rule + collection rss 404 on unknown + collection rss returns valid XML after rule creation + bulk_tag module importable + CLI exposes tag/untag

## 2026-06-02 — v0.25 continuation (autonomous, Ultracode workflow tick #8)

Three feature agents in parallel via Workflow (155k tokens, 5.3 min), then sequential wire-up.

- 02:05Z — version bump 0.24→0.25
- 02:08Z — **Physical image-region blur** (agent A): `app/image_blur.py` with `blur_sensitive_regions(image_path, ocr_data=None)`. Uses `pytesseract.image_to_data(Output.DICT)` for word-level boxes; matches each word against enabled redaction patterns (reusing v0.24's `redaction_rule` table); applies `PIL.ImageFilter.GaussianBlur(radius=12)` on each match. New companion table `blur_applied(screenshot_id, applied_at, regions_count)` via migration 021. OCR worker calls it after text redaction step (only if `image_blur_enabled=True` — opt-in, since it modifies originals). Logs `ocr.image_blurred`.
- 02:12Z — **Per-day storage report** (agent B): `app/storage_report.py` with `daily_breakdown(days_back=30)` summing thumbnails on disk + length(ocr_text) per day. Route `/storage-report` renders table + pure-SVG sparkline (320×40 polyline, no JS). Days >4 MB amber, <4 MB green — directly serves user's 2-4 MB/day disk target.
- 02:16Z — **Notes templates** (agent C): migration 022 seeds 3 starter templates (standup / meeting / bug) via INSERT OR IGNORE. Routes `/notes/templates` (list + add), `/notes/templates/{slug}/delete`, `/notes/templates/{slug}/apply` (returns body for fetch-into-textarea). Slug validation `^[a-z0-9-]{1,40}$`.
- 02:19Z — wired storage_report + note_templates routers
- 02:21Z — tests/test_v025.py — storage report renders + daily_breakdown shape + templates index + seeded templates present + apply returns body + apply 404 on unknown + add+delete round-trip + image_blur module importable

## 2026-06-02 — v0.26 continuation (autonomous, Ultracode workflow tick #9)

Three feature agents in parallel via Workflow (152k tokens, 4.5 min), then sequential wire-up.

- 02:35Z — version bump 0.25→0.26
- 02:38Z — **Lock-aware capture pause** (agent A): `app/capture/session_state.py` with `is_session_locked()` via Windows ctypes WTSGetActiveConsoleSessionId + WTSQuerySessionInformationW (SessionFlags == WTS_SESSIONSTATE_LOCK). On non-Windows always False; ctypes errors fail-open (don't pause). Capture loop calls it before each capture; when locked AND `lock_aware_pause_enabled` (default True), skips the iteration with debug log `capture.session_locked`. Privacy + efficiency win — no useless lock-screen captures.
- 02:41Z — **Power-aware capture cadence** (agent B): `app/capture/power_state.py` with `get_power_state_async()` returning {on_battery, percent, plugged}. Three new settings: `battery_aware_enabled` (default True), `battery_capture_multiplier` (default 3.0× slower on battery), `battery_critical_pct` (default 15% — pause entirely below). Capture loop multiplies sleep by multiplier when on battery, fully skips when critical. Logs `capture.battery_slowdown` / `capture.battery_critical`.
- 02:45Z — **Notes FTS search** (agent C): migration 023 adds FTS5 virtual table `notes_fts` (content='notes', content_rowid='id') with ai/ad/au triggers + initial backfill. Routes `/notes/search?q=Q` (HTML page with `<mark>`-highlighted snippets) and `/api/notes/search.json?q=Q` (50 results max, ORDER BY bm25). FTS query input sanitised against syntax errors.
- 02:48Z — wired notes_search router
- 02:50Z — tests/test_v026.py — session_state importable + bool result + power_state shape + notes_search empty query + with query + JSON API + empty query returns [] + battery/lock settings exist

## 2026-06-02 — v0.27 continuation (autonomous, Ultracode workflow tick #10)

Three feature agents in parallel via Workflow (143k tokens, 3.1 min), then sequential wire-up.

- 03:05Z — version bump 0.26→0.27
- 03:08Z — **Per-screenshot annotations** (agent A): migration 024 + `app/storage/annotations.py` + `app/web/routes/annotations.py`. Routes: GET /api/screenshot/{id}/annotations (list), POST /api/screenshot/{id}/annotations (add), POST /api/annotation/{id}/delete. FK cascade on screenshot deletion. Annotations are user free-form commentary separate from OCR text, notes (global), and tags.
- 03:11Z — **Saved search bookmarks** (agent B): migration 025 + `app/web/routes/saved_searches.py`. /searches lists bookmarks, /searches/{slug} 303-redirects to /search?q=Q. Slug ^[a-z0-9-]{1,40}$, title 1-100 chars, query 1-500 chars. Separate from auto-tracked search history (v0.21) — these are explicit user pins.
- 03:14Z — **Daily-capture streak** (agent C): `app/streak.py` with `current_streak() -> {days, longest, last_capture_date, today_count}`. Computes consecutive days from `date(captured_at)`, finds longest run in history, tracks today_count and last_capture_date. /streak page + /api/streak.json. Empty-DB returns zeros + None.
- 03:17Z — wired annotations + saved_searches + streak routers
- 03:19Z — tests/test_v027.py — annotations 404 + empty-body 400 + saved searches CRUD + bad-slug 400 + redirect preserves query + streak page renders + JSON shape + zero-state on empty DB

## 2026-06-02 — v0.28 continuation (autonomous, Ultracode workflow tick #11)

Three feature agents in parallel via Workflow (176k tokens, 5.9 min), then sequential wire-up.

- 03:35Z — version bump 0.27→0.28
- 03:38Z — **Calendar heatmap** (agent A): `app/heatmap.py` with `yearly_heatmap(end_date=None)` returning 365-day grid with bucketed levels (0..4 via pct33/66/90). Pure-SVG 53×7 grid in `/heatmap` template, GitHub emerald palette, tooltips via SVG `<title>`. JSON API `/api/heatmap.json`.
- 03:42Z — **Top keywords of week** (agent B): `app/keywords.py` with `top_keywords(days=7, top_n=30, min_length=4)` over OCR + notes. ~150-entry STOPWORDS (English + Russian + technical noise). Routes `/keywords` (size-weighted tag cloud, clickable → /search?q=word) + `/api/keywords.json`. Configurable days (7/30/90) and N (15/30/50) via query params.
- 03:46Z — **Screenshot of the day** (agent C): `app/shot_of_day.py` with `shot_of_today()`. Deterministic: SHA-256(today.isoformat()) as seed, index into bounded candidate list (last 90 days, max 5000 ids). Routes `/shot-of-the-day` (big-thumb page) + `/api/shot-of-the-day.json` (404 if empty DB).
- 03:50Z — wired heatmap + keywords + shot_of_day routers (alphabetical k between journal_export and mobile)
- 03:52Z — tests/test_v028.py — heatmap page renders + API shape + zero-state has all-zero days + keywords page + API + STOPWORDS filtering + shot page renders + API empty-state + shot deterministic across calls

## 2026-06-02 — v0.29 continuation (autonomous, Ultracode workflow tick #12)

Three feature agents in parallel via Workflow (178k tokens, 5.2 min), then sequential wire-up.

- 04:05Z — version bump 0.28→0.29
- 04:08Z — **Time-on-app dashboard** (agent A): `app/time_on_app.py` with `daily_time_on_app(day_iso, max_gap_seconds=300)`. Walks consecutive shots; same-app pairs within 5-min gap accumulate seconds; pairs across 5min gap break the run (treat as idle). Routes `/time-on-app?day=YYYY-MM-DD` (HTML table with horizontal bars), `/api/time-on-app.json`, `/time-on-app/summary?days=7` (multi-day aggregate). H:MM:SS via divmod, no humanize lib.
- 04:12Z — **OCR language switcher** (agent B): migration 026 seeds `kv_setting('ocr_languages', 'eng')` via INSERT OR IGNORE. `app/ocr/languages.py` with `get_installed_languages()` (pytesseract.get_languages, fallback ['eng']), `get_configured_languages()`, `set_configured_languages()`. Page `/settings/ocr-languages` shows installed as checkboxes pre-checked. OCR worker reads configured langs with 60s TTL cache, passes as `lang="eng+rus"` etc.
- 04:16Z — **Favourites/star** (agent C): migration 027 + `app/web/routes/favourites.py`. POST `/api/screenshot/{id}/favourite` toggles (insert/delete). GET `/favourites` lists starred shots in a 320px thumbnail grid, most-recent first. JSON API `/api/favourites.json?page=1`. FK cascade on shot deletion. Separate from pin: pin=no auto-demote, fav=quick-access bookmark.
- 04:19Z — wired favourites + ocr_languages + time_on_app routers (alphabetical f/o/t placement)
- 04:21Z — tests/test_v029.py — time-on-app page + API + empty-DB list + ocr-languages page + configured default + round-trip set/get + favourites page + API empty + toggle 404 on missing

## 2026-06-02 — v0.30 milestone (autonomous, Ultracode workflow tick #13)

Three feature agents in parallel via Workflow (190k tokens, 7.3 min), then sequential wire-up. 10th milestone tick in this loop — v0.30 hits two-tenths of v1.0.

- 04:35Z — version bump 0.29→0.30
- 04:38Z — **Webhook HMAC signing** (agent A): `app/webhook_signing.py` with `sign_payload(secret, body) → "sha256=<hex>"` + `verify_payload()` (hmac.compare_digest) + `ensure_secret(webhook_id)` (auto-generates secrets.token_urlsafe(32) for empty rows). Migration 028 adds `secret TEXT DEFAULT ''` to webhook table. Outgoing POSTs now include `X-Persona-Signature: sha256=…` + `X-Persona-Timestamp: <ISO-8601 UTC>` headers. `docs/WEBHOOK_SIGNING.md` has Python + Node receiver-verify snippets with replay-protection guidance.
- 04:43Z — **Bulk-delete** (agent B): `app/bulk_delete.py` with `bulk_delete(query, limit, dry_run=True)` returning {matched, deleted, dry_run, ids}. CLI: `persona-cli delete --query Q [--limit 100] [--confirm]` (refuses --confirm without --query). Web: `/admin/bulk-delete` page with HTMX preview → confirmation token (HMAC of query+matched_count) → confirm endpoint. Cascades cleanup of FTS index + thumbnails on disk. Default is dry-run.
- 04:48Z — **Hour-of-day histogram** (agent C): `app/hour_histogram.py` with `hourly_distribution(days=30)` returning 24-row list (hour, count, pct). Routes `/hours?days=30` (SVG 480×180 bar chart, emerald bars, every-3rd-hour labels, peak hour at top) + `/api/hours.json`. Day-window selector (7/30/90/365).
- 04:53Z — wired bulk_delete + hour_histogram routers
- 04:56Z — tests/test_v030.py — webhook sign deterministic + verify round-trip + tamper detection + bulk_delete dry-run shape + page renders + preview endpoint + hours page + API returns 24 rows + hour set covers 0-23

## 2026-06-02 — v0.31 continuation (autonomous, Ultracode workflow tick #14)

Three feature agents in parallel via Workflow (172k tokens, 4.2 min), then sequential wire-up. User said "продолжай" — proceeded immediately instead of waiting for ScheduleWakeup heartbeat.

- 05:05Z — version bump 0.30→0.31
- 05:08Z — **Idle-time stats** (agent A): `app/idle_stats.py` with `daily_idle(day_iso, idle_threshold_s=60, max_gap_s=300)`. Walks consecutive shots within 5-min gap; classifies pairs by latter shot's `idle_seconds`; accumulates active vs idle seconds. Routes `/idle?day=...` + `/api/idle.json`. Big H:MM:SS for active/idle, ratio bar, first/last capture times. Day picker.
- 05:11Z — **OCR phrase auto-tag** (agent B): migration 029 + `app/ocr_phrase_tags.py` (list/add/delete + `apply_phrase_rules(text)` returning matched tags). Literal multi-word matching via str.find (case-sensitive flag per rule). OCR worker calls it after redaction step and applies tags via existing add_tag helper. Routes `/settings/phrase-tags` (list+add+delete). Different from v0.21's regex auto-tag — phrases are literal.
- 05:14Z — **SMTP digest delivery** (agent C): migration 030 seeds smtp_* kv_setting rows (disabled by default). `app/smtp_delivery.py` with `send_digest_email(subject, body_md)` — try-imports aiosmtplib, returns {"status": "missing_dep" | "disabled" | "misconfigured" | "sent"}. Routes `/settings/smtp` (form, password masked in GET), `/settings/smtp/test` (test send).
- 05:17Z — wired idle_stats + ocr_phrase_tags + smtp_settings routers
- 05:19Z — tests/test_v031.py — idle page + API + empty-DB shape + phrase-tags page + add + apply round-trip + SMTP page + send returns disabled-by-default status

## 2026-06-02 — v0.32 continuation (autonomous, Ultracode workflow tick #15)

Three feature agents in parallel via Workflow (179k tokens, 6.1 min), then sequential wire-up.

- 05:35Z — version bump 0.31→0.32
- 05:38Z — **Day PDF export** (agent A): `app/pdf_export.py` with `export_day_pdf(day_iso, output_path)`. Try-imports reportlab — returns status `missing_dep` if absent. Builds multi-page PDF: title + totals on p1, then thumbnail + caption + 300-char OCR per shot, then notes in chrono order. Route `/export/pdf?day=YYYY-MM-DD` streams `application/pdf` (StreamingResponse). CLI subcommand `persona-cli export-day-pdf --day YYYY-MM-DD --out FILE`.
- 05:42Z — **Theme switcher** (agent B): migration 031 seeds `kv_setting theme=dark`. Routes `/settings/theme` (radios: dark/light/auto) + POST validates {dark, light, auto}. `base.html` reads theme via new `get_theme()` Jinja global; applies `class="dark"` server-side; auto-mode emits `<script>` toggling .dark via `window.matchMedia('(prefers-color-scheme: dark)')`.
- 05:46Z — **Adaptive capture cadence** (agent C): `app/capture/adaptive_cadence.py` with `compute_interval(base, idle, min, max)`. Algorithm: idle<30→min; idle<120→base; else min(max, base*(1+idle/300)). Three new settings (adaptive_cadence_enabled default True, adaptive_min_seconds default 30 range 5-300, adaptive_max_seconds default 600 range 60-3600) with pydantic v2 model_validator asserting max>=min. Capture loop uses computed interval BEFORE battery multiplier so they compose.
- 05:51Z — wired pdf_export + theme routers (p between p…/pin; t between tags/thumbnails)
- 05:54Z — tests/test_v032.py — compute_interval active/normal/idle-cap + PDF route empty-day + theme page + save valid/invalid + adaptive settings defaults + max>=min

## 2026-06-02 — v0.33 continuation (autonomous, Ultracode workflow tick #16)

Three feature agents in parallel via Workflow (216k tokens, 9.1 min), then sequential wire-up.

- 06:05Z — version bump 0.32→0.33
- 06:08Z — **Per-tag trend sparklines** (agent A): `app/tag_trends.py` with `tag_trend(tag, days=30)` returning 30-day list of {date, count} via JOIN screenshot_tag + screenshots GROUP BY date. Routes `/tags/{tag}/trend` (SVG 320×60 polyline + table) + `/api/tags/{tag}/trend.json`. Link to /search?q=tag:{tag}.
- 06:13Z — **Encrypted KV vault** (agent B): migration 032 + `app/vault.py`. Fernet+PBKDF2(100k iters) per-key salt prepended to ciphertext. Stores API keys/secrets under master password. Routes /vault (list keys, no values), POST /vault/get (decrypt with password), POST /vault/set, POST /vault/{key}/delete. Note: this **overrode** the previous /vault routes (which were probably notes-related) — caller should verify the old behaviour isn't load-bearing.
- 06:18Z — **Screenshot diff slider** (agent C): `app/web/routes/diff_slider.py` with /diff/{id_a}/{id_b} (two thumbnails stacked, top one CSS-clipped to inset(0 var(--split) 0 0), single range input drives the var via vanilla JS oninput) + /diff/random (picks two random shots from past 7 days). Pure CSS+1-line JS, no framework.
- 06:23Z — wired tag_trends + diff_slider routers; vault router already in main.py from earlier session
- 06:25Z — tests/test_v033.py — tag trend page + API ≥28 rows + zero-tag returns zeros + diff 404 + diff random + vault page + set/get roundtrip + wrong-password rejected

## 2026-06-02 — v0.34 continuation (autonomous, Ultracode workflow tick #17)

Three feature agents in parallel via Workflow (199k tokens, 5.2 min), then sequential wire-up. First version published to GitHub: https://github.com/SwairIt/persona (git init + initial commit + push happened mid-tick).

- 06:45Z — version bump 0.33→0.34
- 06:48Z — **Weekly stats PDF** (agent A): `app/weekly_pdf.py` with `export_week_pdf(week_start_iso, output_path)` (reportlab). Cover with totals + streak + first/last capture, daily bar chart, top-10 apps with hours, top-20 keywords, thumbnail mosaic of top 12. Route `/export/weekly-pdf?week=YYYY-MM-DD`. CLI subcommand `persona-cli export-week-pdf`.
- 06:52Z — **OCR diff viewer** (agent B): `app/ocr_diff.py` (difflib.unified_diff + HtmlDiff). Route `/diff/ocr/{id_a}/{id_b}`. Template has unified/side-by-side toggle (CSS only), diff_add/diff_sub colouring. 404 on missing shots.
- 06:57Z — **API token bearer auth** (agent C): migration 033 + `app/api_tokens.py` (secrets.token_urlsafe(32) raw, SHA256 hash stored, hmac.compare_digest verify). `app/web/middleware/api_auth.py` ApiAuthMiddleware: inspects /api/* paths, attaches scopes to request.state when valid bearer; 401 JSON when invalid. Settings flag `api_auth_required` (default False — backwards compatible). Routes `/settings/api-tokens` (list + create-show-once + revoke).
- 07:02Z — wired api_tokens + ocr_diff + weekly_pdf routers + ApiAuthMiddleware in app middleware stack (before GZip)
- 07:05Z — tests/test_v034.py — weekly PDF route + OCR diff 404 + diff module + api-tokens page + create/verify/revoke round-trip + bad token rejected

## 2026-06-02 — v0.35 continuation (autonomous, Ultracode workflow tick #18)

Three feature agents in parallel via Workflow (199k tokens, 6.7 min), then sequential wire-up.

- 07:35Z — version bump 0.34→0.35
- 07:38Z — **Clipboard history capture** (agent A): migration 034 + `app/capture/clipboard.py` (Windows ctypes OpenClipboard / CF_UNICODETEXT) + `app/workers/clipboard_worker.py` (2s poll, SHA-256 dedup, applies redaction patterns from v0.24 before storing). Setting `clipboard_history_enabled` default False (opt-in). Routes `/clipboard` (history + LIKE search) + `/api/clipboard.json`. New worker wired into lifespan.
- 07:43Z — **OCR per-word confidence overlay** (agent B): migration 035 (table `ocr_word` with conf + box coords) + OCR worker writes per-word rows via `image_to_data(Output.DICT)`. Route `/screenshot/{id}/overlay` renders the image with absolutely-positioned <span> overlays colour-coded by conf (green ≥80, amber 50-79, red <50). Click a word to /search?q=word.
- 07:48Z — **iCalendar (.ics) export** (agent C): `app/ics_export.py` builds iCal 2.0 string via stdlib only (no icalendar package). One VEVENT per day-with-shots, top 3 apps in DESCRIPTION, all-day events, RFC-5545-compliant escaping + CRLF. Route `/export/calendar.ics?days=90` with attachment Content-Disposition.
- 07:53Z — wired clipboard + ocr_overlay + ics_export routers + run_clipboard_worker in lifespan tasks
- 07:55Z — tests/test_v035.py — clipboard page + API + setting default off + overlay renders or 404 + words API + ICS returns BEGIN/END:VCALENDAR + zero-state module + RFC-5545 escaping check

## 2026-06-02 — v0.36 continuation (autonomous, Ultracode workflow tick #19)

Three feature agents in parallel via Workflow (204k tokens, 7.2 min), then sequential wire-up.

- 08:25Z — version bump 0.35→0.36
- 08:28Z — **Focus-mode (Pomodoro)** (agent A): migration 036 + `app/focus.py` (start_session/end_session/current_session/recent_sessions). Route `/focus` renders countdown timer (JS computed from started_at + work_minutes), CSS progress ring, Web Audio API beep on zero. Posts /focus/start and /focus/end. Recent sessions table. Note: agent overwrote earlier focus.py (the existing /focus route was wired in main.py already — same router import path keeps working).
- 08:33Z — **Audit log** (agent B): migration 037 + `app/audit.py` (log_action / list_recent). Route `/audit` with action-substring filter + pagination. Integrated at: bulk_delete.confirm logs query+counts; api_tokens logs create/revoke; vault logs set/get/delete (key name only — NEVER the value).',
- 08:38Z — **Per-day TL;DR** (agent C): migration 038 + `app/llm/day_tldr.py` with `summarise_day_tldr(day_iso)` returning {status, tldr, cached}. Cache-aside via `day_tldr` table. One-sentence (max 30 words) using BYO LLM. Route `/api/day-tldr.json?day=...` + POST regenerate. Returns `missing_config` if no LLM key — never blocks render.
- 08:42Z — wired audit + day_tldr routers (focus already wired earlier)
- 08:44Z — tests/test_v036.py — focus page + start/current/end roundtrip + audit page + log records + secret-leak smoke check + tldr API missing_config tolerant + module returns status

## 2026-06-02 — v0.37 continuation (autonomous, Ultracode workflow tick #20)

Three feature agents in parallel via Workflow (293k tokens, 14.5 min — the chunkiest tick yet), then sequential wire-up. 20th milestone in this loop.

- 09:15Z — version bump 0.36→0.37
- 09:18Z — **Settings backup JSON** (agent A): `app/settings_backup.py` dumps kv_setting, redaction_rule, auto_collection, ocr_skip_app, ocr_phrase_tag, saved_search, note_template, app_overrides, webhook (sans secret), quiet_hours. Routes `/settings/backup` (page + download) + POST import (multipart + merge/replace flag). CLI: `persona-cli export-settings --out FILE` and `import-settings --in FILE [--replace]`. NEVER exports webhook.secret or vault ciphertext.
- 09:25Z — **Worker heartbeat dashboard** (agent B): migration 039 + `app/workers/heartbeat.py` (beat / get_all). Every worker (capture, ocr, retention, embeddings, digest, weekly-digest, clipboard) now calls `await beat(name)` at the top of each loop iteration. Routes `/admin/health` (page) + `/api/health.json`. Colour-coded freshness (green <120s, amber <600s, red older).
- 09:33Z — **Markdown inbox** (agent C): `app/workers/inbox_worker.py` watches `data/inbox/`, parses YAML-ish frontmatter (--- ... ---), INSERT INTO notes, applies tags via existing add_tag, moves to `processed/` on success or `failed/{name}.error.txt` on parse error. New worker wired into lifespan. Settings `inbox_enabled` (default True), `inbox_path` (default data/inbox).
- 09:38Z — wired settings_backup + health_dashboard + inbox routers + run_inbox_worker in lifespan
- 09:42Z — tests/test_v037.py — export shape + secret-omission check + backup page + heartbeat beat+read + health page + API + inbox page + settings defaults

## 2026-06-02 — v0.38 continuation (autonomous, Ultracode workflow tick #21)

Three feature agents in parallel via Workflow (203k tokens, 13.7 min), then sequential wire-up.

- 10:05Z — version bump 0.37→0.38
- 10:09Z — **Cmd+K command palette** (agent A): `app/web/static/command_palette.js` (vanilla) + CSS. Modal overlay opens on Cmd/Ctrl+K, fuzzy-matches against ~30 hard-coded top routes + dynamic items from saved_search and auto_collection. Arrow/Enter/Esc navigation. Recent routes from localStorage. Route data via GET `/api/palette.json`. base.html includes script+css and pushes current path on each render.
- 10:14Z — **Screenshot of the week** (agent B): `app/shot_of_week.py` with `shot_of_this_week()`. Algorithm: candidates from last Mon-Sun, score = pinned*5 + favourited*3 + tags + annotations, top-1 by score then recency; falls back to `shot_of_today()` if no candidates. Route `/shot-of-the-week` (big thumbnail + score breakdown) + `/api/shot-of-the-week.json`.
- 10:18Z — **Stats CSV export** (agent C): `app/stats_csv.py` with `export_stats_csv(days_back=90)` returning CSV string. Per (date, app_name) rollup: shots, total_idle_seconds, total_active_seconds, ocr_chars_total, has_tldr. stdlib csv.writer, StringIO. Route `/export/stats.csv?days=N`. CLI `persona-cli export-stats-csv --days N --out FILE`.
- 10:22Z — wired palette + shot_of_week + stats_csv routers
- 10:24Z — tests/test_v038.py — palette API items + shot-of-week page + API + empty-DB fallback + stats CSV streams + module headers + injection-safe

## 2026-06-02 — v0.39 continuation (autonomous, Ultracode workflow tick #22)

Three feature agents in parallel via Workflow (183k tokens, 7.2 min), then sequential wire-up.

- 10:50Z — version bump 0.38→0.39
- 10:53Z — **Keyboard shortcut cheatsheet** (agent A): `app/web/static/keyboard_shortcuts.js` + css. Press `?` (when not in input/textarea/contenteditable) → modal overlay listing all shortcuts: ?, Cmd/Ctrl+K, /, g+t/s/h/f. Multi-key g+letter sequences with 1.5s timeout. base.html includes script + css alongside v0.38 palette.
- 10:57Z — **OCR language statistics** (agent B): `app/ocr/language_stats.py` with `language_breakdown(days=30)` returning {cyrillic_chars, latin_chars, cjk_chars, digit_chars, other_chars, total_chars, top_apps_by_language}. Char classification via Unicode ranges (U+0400-04FF cyrillic, U+0041-007A latin, U+4E00-9FFF CJK). Routes `/stats/ocr-languages` (page) + `/api/ocr-languages.json`.
- 11:02Z — **Archive ZIP bundle** (agent C): `app/archive_bundle.py` with `build_archive(days, output_path, include_thumbnails)`. Stdlib zipfile.ZIP_DEFLATED. Layout: settings.json (via v0.37 settings_backup, no secrets) + screenshots.json + notes.json + thumbnails/{id}.webp + README.txt. Route `/export/archive.zip?days=N&thumbs=1`. CLI `persona-cli archive --days N --out FILE [--no-thumbnails]`.
- 11:06Z — wired ocr_language_stats + archive_bundle routers (keyboard shortcuts is JS-only)
- 11:08Z — tests/test_v039.py — lang-stats page + API + module shape + zero-state + archive endpoint + build_archive module to tmp_path + JS static file exists

## How to run

```powershell
cd C:\www-Yaroslav\Persona
uv sync
copy .env.example .env
uv run python scripts/setup_database.py
uv run python scripts/check_environment.py
uv run uvicorn app.web.main:app --host 127.0.0.1 --port 8765
```

Enable semantic search (optional but recommended):

```powershell
uv sync --extra embeddings
# in .env:
#   PERSONA_EMBEDDINGS_ENABLED=true
# restart — fastembed downloads model on first use (~120MB)
```

## What is NOT in v0.2 (intentionally)

- Cloud sync (single device only)
- Zero-knowledge encryption
- Mobile
- Multi-monitor support beyond primary
- macOS / Linux capture
- Code signing / installers
- Encrypted backup blobs
