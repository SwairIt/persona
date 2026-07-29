# Changelog

All notable changes to Persona are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.29.1] - 2026-07-29

### Changed
- The PC worker preloads the server-selected chat model before accepting real
  work, and Telegram keeps it resident in Ollama indefinitely to remove cold
  model loads after idle periods.
- Local Ollama requests explicitly disable hidden thinking output, preserving
  the current direct-response behaviour if a thinking-capable model is selected.
- Worker logs now record model load latency, prompt throughput and generation
  tokens per second for evidence-based hardware and model tuning.

## [2.29.0] - 2026-07-29

### Changed
- Telegram worker jobs now upload one complete answer instead of blocking local
  Ollama with one WAN request and SQLite transaction per generated token.
- Short requests can use a 2048-token Ollama context tier, while longer
  prompts still scale automatically to 4096, 8192, 16384 or 32768 tokens.
- Telegram removes a duplicate identity preamble, bounds recent transcript
  context to 800 characters and uses smaller relevance-focused memory blocks.
- Private Telegram replies are capped at 96 tokens; group replies retain their
  existing 64-token conversational budget.
- Cross-process worker wake-up fallback dropped from two seconds to 250 ms.

## [2.28.0] - 2026-07-29

### Added
- The living prompt now compiles owner preferences into a conflict-free,
  bounded layer of at most six rules and 700 characters while preserving
  every effective prompt revision in version history.
- Telegram detects near-duplicates of recent Persona replies and performs one
  private context-aware regeneration before persisting the answer.

### Changed
- Ollama generation uses sampling and repetition controls tuned to reduce
  stale phrasing without adding prompt context or slowing the normal path.
- Persona explicitly anchors each reply to the latest message and stops the
  previous conversational line when told to stop.

## [2.27.8] - 2026-07-29

### Changed
- Persona addresses every Telegram participant informally with «ты» by
  default; formal speech is reserved for an explicit owner instruction.

## [2.27.7] - 2026-07-29

### Fixed
- Persona no longer narrates its own politeness, constructiveness or quality
  of interaction; Telegram removes those meta-assistant sentences and uses a
  terse human acknowledgement when nothing else remains.

## [2.27.6] - 2026-07-29

### Changed
- Telegram now uses a dedicated sub-1800-character personality core, three
  recent turns, compact memory recall and 64/128-token group/private limits.
  Full memory remains stored and searchable outside the online prompt budget.

## [2.27.5] - 2026-07-29

### Changed
- Persona's living prompt no longer contains a helpful-assistant identity or
  a professional-help goal. Direct requests to dismiss, roast or swear at
  someone must be fulfilled in the first sentence without a softer substitute.

### Fixed
- Telegram strips stock suggestions to review the approach or avoid insults
  when they contradict the requested conversational role.

## [2.27.4] - 2026-07-29

### Changed
- Long LLM maintenance waits for ten minutes of worker headroom, keeping
  Telegram responsive during intermittent conversations as well as rapid chat.

## [2.27.3] - 2026-07-29

### Changed
- Telegram uses an aggressive low-latency context profile with five recent
  turns, compact participant identity and shorter conversational outputs.
- Long summaries are capped at 320 tokens and wait for a quiet worker window.

### Fixed
- Live Telegram jobs now atomically outrank background work and preempt a
  running summary or impulse instead of waiting behind it for several minutes.
- Telegram turns no longer launch the legacy 2000-token conversation summary
  that starved the single PC Ollama worker after every reply.

## [2.27.2] - 2026-07-29

### Changed
- Persona now enters requested roles immediately, keeps a deliberately sharp
  tone without apologetic backpedalling, and uses direct refusals only when an
  action is genuinely unavailable.
- Telegram conversations use compact history, recall, ambient context and
  output budgets; casual owner messages omit unused tool schemas.
- The PC worker now receives an adaptive Ollama context window and keeps the
  selected model warm between requests.

### Fixed
- Telegram output removes stock tone apologies and constructive-assistant
  disclaimers that contradicted an active role.

## [2.27.1] - 2026-07-29

### Fixed
- Telegram output now removes stock support closings and patronising praise
  even when the local model ignores the living-character prompt.

## [2.27.0] - 2026-07-29

### Added
- Persona now uses an automatically adapting living-character prompt with
  versioned modes and owner-only learned style preferences.
- Added an owner-only prompt history page with full snapshots and rollback.

### Changed
- The adaptive personality replaces the old support-assistant style and uses
  more varied Telegram generation settings.

### Fixed
- Site copilot SSE now sends heartbeats while the PC worker prepares its first
  token, preventing proxy disconnects and cancelled jobs.

## [2.26.2] - 2026-07-29

### Fixed
- Telegram dialogue filtering now recognises participant labels decorated with
  emoji and blocks broader multi-speaker role-play output.

## [2.26.1] - 2026-07-29

### Fixed
- Telegram replies no longer start with Persona's own `Персик:`/`Persona:` speaker label.

## [2.26.0] - 2026-07-29

### Added

- Stable Telegram people registry keyed by numeric account id, with retained
  messages, person-scoped self-statements, owner-only list/detail pages and
  server-verified identity context for every group turn.
- Persistent bottom-right site copilot that explains pages, finds settings and
  can apply a small allowlist of explicit owner setting requests.

### Fixed

- Make the Telegram binding the immutable sole owner/creator authority and
  prevent participant claims from changing it.
- Prevent Persona from inventing multi-speaker scripts or answering on behalf
  of Indi, Claude and other group participants.

## [2.25.0] - 2026-07-29

### Fixed

- Prioritise live Telegram/web conversations over autonomous background LLM
  work so a slow single-PC worker does not starve user replies.
- Cancel abandoned durable LLM jobs on request timeout or disconnect.
- Raise the no-token timeout to five minutes and shorten Telegram generations
  for slower local models.
- Make the PC worker stop an Ollama stream after the server rejects a cancelled
  job instead of blocking the queue until generation completes.

## [1.0.0] - 2026-06-03

First stable public release. Codifies the v0.x autonomous-loop work into a
production-ready single-user personal-AI-memory app.

### Added
- Stable public API surface across timeline, search, capture, OCR, LLM, export, sharing, and admin areas.
- One-shot `/setup` wizard as the canonical first-run experience.
- Programmatic `/api/query` JSON endpoint and `/features` discovery index.
- Full LICENSE (AGPL-3.0-or-later) and audited public README.

### Changed
- Documentation overhaul: README rewritten to reflect the full feature surface.
- All version metadata bumped to `1.0.0` across `app/__init__.py` and `pyproject.toml`.
- Default settings tuned for the "2-4 MB per active day" disk target.

### Security
- Final review pass on webhook HMAC, vault encryption (PBKDF2 600k iters), API tokens, and audit logging.

## [0.99.0] - 2026-06-03

### Added
- Quick-pin keyboard shortcut on screenshot detail.
- Autotag suggester powered by BYO LLM.

### Changed
- README overhaul as the pre-v1 polish pass.

## [0.98.0] - 2026-06-03

### Added
- Bulk untag UI on search results.
- LLM usage stats dashboard.
- Shot groups (logical grouping of related captures).

## [0.97.0] - 2026-06-03

### Added
- Corpus-wide search across screenshots, notes, and tags.
- SVG sparkline component reused on stats pages.
- Dedup cluster admin page.

## [0.96.0] - 2026-06-03

### Added
- Top-100 dashboard (most-frequent apps, windows, tags).
- Chrome/Firefox browser extension (full MV3 build).
- Tag merge wizard with conflict preview.

## [0.95.0] - 2026-06-03

### Added
- One-click "Copy digest" button on digest pages.
- Fullscreen screenshot view.
- Saved facet sets (reusable search-filter bundles).

## [0.94.0] - 2026-06-03

### Added
- PWA manifest and service worker for installable offline use.
- Drag-and-drop image import from desktop.
- Inline OCR text editing on screenshot detail.

## [0.93.0] - 2026-06-03

### Added
- Day timeline JSON API.
- Reduce-motion accessibility toggle.
- Top-words CSV export.

## [0.92.0] - 2026-06-03

### Added
- Per-tag OCR text export.
- OCR history snapshots (preserve prior OCR runs).

### Security
- KDF iteration count bumped to 600,000 for vault and backup encryption.

## [0.91.0] - 2026-06-03

### Added
- Backup verification CLI (`persona-cli backup-verify`).
- Per-tag gallery view.
- Per-app calendar view.

## [0.90.0] - 2026-06-03

### Added
- Audit-log replay tool.
- OCR words TSV export.
- Capture-rate guard (warn + auto-pause when capture rate spikes).

## [0.89.0] - 2026-06-03

### Added
- Streak badges on the streak page.
- Weekly stats share-card (sharable PNG).
- Sticky search bar that follows scroll.

## [0.88.0] - 2026-06-03

### Added
- OCR phone-number detection rule.
- "Tag all from filter" bulk action.
- Retention trend chart on Stats.

## [0.87.0] - 2026-06-03

### Added
- API token scopes (read / write / admin granularity).
- OCR email-address detection rule.
- Live count widget on the header.

## [0.86.0] - 2026-06-03

### Added
- Settings JSON API (`GET/PUT /api/settings`).
- Phrase frequency report.
- Custom dashboard widgets with drag-to-arrange.

## [0.85.0] - 2026-06-03

### Added
- Focus-mode blocklist (pause capture on listed apps during focus).
- Per-feed authentication tokens for RSS endpoints.
- Screenshot crop tool.

## [0.84.0] - 2026-06-03

### Added
- App icons surfaced in search results.
- Recycle bin batch operations.
- Thumbnail-regeneration CLI.

## [0.83.0] - 2026-06-03

### Added
- Lock CLI (lock vault / quit capture from terminal).
- OCR URL linkify (auto-link detected URLs in OCR text).
- Slack-style daily summary template.

## [0.82.0] - 2026-06-03

### Added
- Stickers gallery (curated thumbnails).
- Share-collection ZIP download.
- Icon batch-refresh job.

## [0.81.0] - 2026-06-03

### Added
- Dashboard tile customisation (per-user widget layout).
- OCR translate (one-click via BYO LLM).
- Recycle bin share-restore (recover already-shared items).

## [0.80.0] - 2026-06-03

### Added
- Search snippet highlighting (`<mark>` around hits).
- Audit log filter UI.
- Embeddings statistics page (coverage, model, dimensions).

## [0.79.0] - 2026-06-02

### Added
- Kanban CSV export.
- Image rotation in gallery viewer.
- Share-collection PDF export.

## [0.78.0] - 2026-06-02

### Added
- Day OCR diff (compare two days' OCR corpora).
- Grayscale display mode.
- Monthly stats CSV export.

## [0.77.0] - 2026-06-02

### Added
- Notes link checker (flag broken URLs in notes).
- Share-link visit analytics.
- OCR find/replace across the corpus.

## [0.76.0] - 2026-06-02

### Added
- Annotations NDJSON export.
- Nightly encrypted DB backup worker.
- Audit timeline view.

## [0.75.0] - 2026-06-02

Milestone release (3/4 toward v1).

### Added
- Ping heatmap (network availability over time).
- Vision-OCR replace (re-OCR a frame using LLM vision model).
- Monthly digest prompt template.

## [0.74.0] - 2026-06-02

### Added
- Bulk "add to collection" action.
- OCR copy-as-markdown.
- Idle-ping notifications.

## [0.73.0] - 2026-06-02

### Added
- Framed-export watermark option.
- Weekly digest RSS feed.
- Settings diff viewer (compare snapshots).

## [0.72.0] - 2026-06-02

### Added
- OCR highlight overlay on screenshot detail.
- Framed-export (border + caption around a shot).
- Day-end auto-summary.

## [0.71.0] - 2026-06-02

### Added
- LLM provider switcher in settings (Anthropic / OpenAI / Groq).
- Multi-day diff view.
- Zoom deep-link (preserve zoom state in URL).

## [0.70.0] - 2026-06-02

### Added
- Monthly digest card on home.
- Per-shot lock (encrypt a single screenshot).
- RSS index page.

## [0.69.0] - 2026-06-02

### Added
- App health dashboard.
- OCR rerun-N (requeue last N shots).
- Multi-shot ZIP share-link.

## [0.68.0] - 2026-06-02

### Added
- Semantic-similar widget on screenshot detail.
- Monthly digest generator.
- CORS preflight handling on capture endpoints.

## [0.67.0] - 2026-06-02

### Added
- Per-app capture-skip rules (extends process-whitelist).
- OCR copy-to-clipboard button.
- Grid sort options.

## [0.66.0] - 2026-06-02

### Added
- Search autocomplete using FTS history.
- Sticky export toolbar.
- Web Push notifications.

## [0.65.0] - 2026-06-02

### Added
- Unified dashboard combining stats + recent activity.
- OCR length chart.
- Bulk-select toolbar on search results.

## [0.64.0] - 2026-06-02

### Added
- Webhook retry queue with backoff.
- OCR force-reindex CLI.
- Sticky notes (always-visible note overlay).

## [0.63.0] - 2026-06-02

### Added
- Bulk-pin from query.
- OCR error-rate panel.
- App groups (logical bundles of related apps).

## [0.62.0] - 2026-06-02

### Added
- Weekly digest share-card (PNG export).
- `/random` route that picks a random past shot.
- OCR language auto-detect.

## [0.61.0] - 2026-06-02

### Added
- Token cloud visualisation.
- Share-link visits CSV export.
- Compact display mode.

## [0.60.0] - 2026-06-02

### Added
- App rename UI (without losing history).
- Idle-time weekly chart.
- Clipboard history facets.

## [0.59.0] - 2026-06-02

### Added
- Floating calendar navigator.
- Anti-FOMO digest mode (qualitative, no counts).
- `sitemap.xml`.

## [0.58.0] - 2026-06-02

### Added
- Query collections (save complex multi-facet queries).
- Custom app icons (override auto-detected).
- Pinmap (map view of pinned shots).

## [0.57.0] - 2026-06-02

### Added
- Weekly stats CSV via email.
- Embeddings re-index button.
- Per-app digest.

## [0.56.0] - 2026-06-02

### Added
- Weekly digest prompt customisation.
- Iframe embed code for share links.
- Diagnostic bundle export (zip of logs + settings).

## [0.55.0] - 2026-06-02

### Added
- Share-link read receipts.
- Tag cleanup CLI.
- OCR via vision LLM (low-confidence fallback).

## [0.54.0] - 2026-06-02

### Added
- Storage savings counter (bytes saved by dedup + tier).
- QR code generation for share links.
- Digest app blocklist (exclude apps from digest).

## [0.53.0] - 2026-06-02

### Added
- Saved-search alerts (notify on new matches).
- Reading mode (distraction-free OCR text view).
- Thumbnail dedup pass.

## [0.52.0] - 2026-06-02

### Added
- OCR colour sampling (extract dominant colours from frame).
- Screenshot dimensions stored on each row.
- Daily email digest.

## [0.51.0] - 2026-06-02

### Added
- Search keyboard navigation (j/k/Enter/slash bindings).

## [0.50.0] - 2026-06-02

Milestone release — half-way to v1.

### Added
- Feature index (`/features`) — auto-generated catalogue grouped by category.
- JSON query API (`POST /api/query`) — programmatic search with `{fts, app, date_from, date_to, tags, kinds, limit}` schema.
- One-shot setup wizard (`/setup`) covering theme, capture, OCR, LLM, retention.

### Security
- `SetupGateMiddleware` redirects fresh installs to `/setup` (allow-listing static, API, events, health).

## [0.49.0] - 2026-06-03

### Added
- Per-tag RSS (`/tags/{name}.rss`).
- Visual diff thumbnails via `ImageChops.difference` and contrast boost.
- Per-app retention overrides with `never_delete` flag.

## [0.48.0] - 2026-06-03

### Added
- Permalinks: 8-char base36 slugs with hit counter, open-redirect-safe.
- Per-day reading time (words and minutes at 250 wpm, with per-app breakdown).
- Tag merge admin tool with dry-run preview.

## [0.47.0] - 2026-06-03

### Added
- Per-day notes timeline at `/notes/day/{day}` with deep-link anchors.
- Duplicate-screenshot suggestions ("Possibly related" via dedup group + pHash).
- Audit-log RSS feed (loopback-only).

## [0.46.0] - 2026-06-03

### Added
- Per-tag colour customisation (validated `#RRGGBB`).
- Image viewer zoom and pan (wheel, drag, pinch, double-click reset).
- Day kanban view grouped by app.

## [0.45.0] - 2026-06-03

### Added
- Per-app icon cache: deterministic 64×64 PNGs with hashed HSL backgrounds.
- Encrypted note bodies (Fernet + PBKDF2 per-note salt) with audit-logged decrypt.
- Retention rule preview showing demotions, deletions, and estimated bytes freed.

## [0.44.0] - 2026-06-03

### Added
- Webhook event filters (`*`, exact, or glob-prefix like `screenshot.*`).
- OCR near-duplicate admin via Jaccard similarity on tokenised OCR text.
- Public-day opt-in pages with private-tag filtering.

## [0.43.0] - 2026-06-02

### Added
- Query syntax help popover (FTS5 cheatsheet behind `?` button).
- Right-click context menu on thumbnails (Pin / Favourite / Open / Tag / Delete).
- Per-shot share link with signed token, TTL, and revoke list.

## [0.42.0] - 2026-06-02

### Added
- Day scrubber video player with play/pause, speed selector, keyboard nav.
- OCR retry queue with conf and empty-text filters.
- Per-day collage PNG export.

## [0.41.0] - 2026-06-02

### Added
- Search facets (app dropdown, date range, repeatable tag filter) composed with FTS5.
- Drag-to-tag UI (HTML5 drag-and-drop of tag chips onto thumbnails).
- Browser bookmarklet that POSTs URL, title, and selection as a note.

## [0.40.0] - 2026-06-02

Milestone release.

### Added
- Live SSE status pill (`/events` text/event-stream replacing setInterval polling).
- Per-day OCR `.txt` export with timestamp/app delimiters.
- Undo bin (soft-delete) with restore, purge, and `recycle_retention_days` setting.

## [0.39.0] - 2026-06-02

### Added
- Keyboard shortcut cheatsheet modal (press `?`).
- OCR language statistics (Cyrillic / Latin / CJK char breakdown via Unicode ranges).
- Archive ZIP bundle export with settings + screenshots + notes + thumbnails.

## [0.38.0] - 2026-06-02

### Added
- Cmd+K command palette with fuzzy-match across ~30 routes plus saved searches.
- Screenshot of the week (scored by pin, favourite, tags, annotations).
- Stats CSV export with per-(date, app) rollup.

## [0.37.0] - 2026-06-02

### Added
- Settings backup JSON (kv_setting, rules, collections, webhooks sans secret).
- Worker heartbeat dashboard with colour-coded freshness at `/admin/health`.
- Markdown inbox worker watching `data/inbox/` for YAML-frontmatter notes.

### Security
- Settings export NEVER includes webhook secrets or vault ciphertext.

## [0.36.0] - 2026-06-02

### Added
- Focus-mode (Pomodoro) with countdown ring and Web Audio beep.
- Audit log (`audit_log` table) integrated with bulk_delete, api_tokens, vault.
- Per-day TL;DR (one-sentence BYO-LLM summary, cache-aside).

## [0.35.0] - 2026-06-02

### Added
- Clipboard history capture (opt-in, 2s poll, SHA-256 dedup, redaction applied).
- OCR per-word confidence overlay (green ≥80, amber 50-79, red <50).
- iCalendar (`.ics`) export, stdlib-only, RFC-5545-compliant.

## [0.34.0] - 2026-06-02

### Added
- Weekly stats PDF with cover, daily chart, top apps/keywords, thumbnail mosaic.
- OCR diff viewer via `difflib.unified_diff` and `HtmlDiff`.
- API token bearer auth (SHA-256 hashed, `hmac.compare_digest`) plus middleware.

## [0.33.0] - 2026-06-02

### Added
- Per-tag trend sparklines (30-day SVG polyline plus table).
- Encrypted KV vault (Fernet + PBKDF2 100k iters, per-key salt) for API keys.
- Screenshot diff slider with CSS-clip overlay and `/diff/random`.

## [0.32.0] - 2026-06-02

### Added
- Day PDF export (reportlab; thumbnails + 300-char OCR + notes).
- Theme switcher (dark / light / auto with `prefers-color-scheme`).
- Adaptive capture cadence based on idle seconds, composed with battery multiplier.

## [0.31.0] - 2026-06-02

### Added
- Idle-time stats per day with active/idle split and ratio bar.
- OCR phrase auto-tag (literal multi-word matches, case-sensitive flag per rule).
- SMTP digest delivery via `aiosmtplib` with disabled-by-default settings.

## [0.30.0] - 2026-06-02

Milestone release.

### Added
- Bulk-delete with HMAC confirmation token, FTS cleanup, and disk-thumbnail cascade.
- Hour-of-day histogram (SVG bar chart with 7/30/90/365-day window selector).

### Security
- Webhook HMAC signing: `X-Persona-Signature: sha256=...` and `X-Persona-Timestamp` headers; secret auto-generated via `secrets.token_urlsafe(32)`.

## [0.29.0] - 2026-06-02

### Added
- Time-on-app dashboard with 5-min idle-gap rule and per-day summary.
- OCR language switcher (`/settings/ocr-languages`) backed by `pytesseract.get_languages`.
- Favourites/star with toggle endpoint and 320px grid view.

## [0.28.0] - 2026-06-02

### Added
- Calendar heatmap (GitHub-style 53×7 SVG grid with bucketed levels).
- Top keywords of the week (~150-entry stopword list, EN+RU+technical).
- Screenshot of the day (deterministic SHA-256 seed from current date).

## [0.27.0] - 2026-06-02

### Added
- Per-screenshot annotations (free-form commentary, separate from notes and tags).
- Saved-search bookmarks with slug + 303 redirect to `/search?q=...`.
- Daily-capture streak (current, longest, today_count, last_capture_date).

## [0.26.0] - 2026-06-02

### Added
- Lock-aware capture pause (Windows ctypes WTS API; fail-open on errors).
- Power-aware capture cadence (battery multiplier 3×; pause at critical 15%).
- Notes FTS5 search with `<mark>`-highlighted snippets.

## [0.25.0] - 2026-06-02

### Added
- Physical image-region blur (Gaussian blur over OCR boxes matching redaction rules).
- Per-day storage report (`/storage-report`) with SVG sparkline and 4 MB amber/green threshold.
- Notes templates (standup / meeting / bug seeded) with apply-into-textarea.

## [0.24.0] - 2026-06-02

### Added
- Bulk-tag CLI (`persona-cli tag --add TAG --query Q`) plus untag.
- RSS feed per auto-collection (`/collection/{slug}.rss`).

### Security
- OCR text redaction (email, credit card, bearer token) applied before storage so secrets never enter FTS5 index.

## [0.23.0] - 2026-06-02

### Added
- Encrypted backup/restore CLI (Fernet + PBKDF2 100k; tarball with DB checkpoint + N days of thumbnails).
- Tag-driven auto-collections (slug-bound; membership computed on read).
- Per-app OCR skip-list (`/settings/ocr-skip`) consulted before invoking Tesseract.

## [0.22.0] - 2026-06-02

### Added
- `persona-doctor`: 12 diagnostic checks (Python, SQLite/FTS5, data dir, schema, Tesseract, embeddings, LLM, disk, capture freshness) with CLI and `/doctor` page.
- Weekly LLM digest (Monday-anchored) with archive page and 30-min polling scheduler.
- `persona-cli capture` subcommand with `--app NAME` override, plus AutoHotkey/PowerToys recipes.

[Unreleased]: https://github.com/SwairIt/Persona/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/SwairIt/Persona/releases/tag/v1.0.0
