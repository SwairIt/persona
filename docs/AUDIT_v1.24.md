# Persona v1.24 — Codebase Audit

Scope: 274 route modules, 24 workers, 107 migrations, 206 templates, ~993 .py/.html files. Read-only audit; no code touched.

---

## 1. Duplicate / near-duplicate routes

### 1a. Mic / capture controls — three places do the same thing
- `app/web/routes/mic_toggle.py:42,48` — `GET/POST /api/audio/mic` (v1.14) flips `audio_capture_paused_live`.
- `app/web/routes/capture_settings.py:85` — `POST /settings/capture` (v1.17) ALSO writes `audio_capture_paused_live` (line 115) PLUS screens / interval / mic-schedule.
- `app/web/routes/audio_settings.py:234,293` — `GET/POST /settings/audio` (v1.11) writes `audio_capture_enabled` + codec/bitrate/VAD/Whisper.

**Recommendation:** `/settings/capture` is already a "unified" page but only covers ON/OFF. Promote it to the canonical settings hub for capture; demote `/settings/audio` to its advanced-knobs sub-page (codec, bitrate, VAD, Whisper). Keep `/api/audio/mic` as the one-click toolbar endpoint only (it's wired into `base.html:189` x-data="micToggle()" — don't break it), but delete the per-flag handlers and have the toolbar POST to a thin shim that reuses the same kv row.

### 1b. Auto-translate settings live alone
- `app/web/routes/auto_translate_settings.py:70,83` — `GET/POST /api/audio/translate` (v1.18) flips `audio_auto_translate_enabled`. JSON-only. No HTML companion.

**Recommendation:** fold the toggle into `/settings/audio` (since it's an audio-pipeline knob); keep the segment-level `/api/audio-segment/{id}/translation.json` separate.

### 1c. Settings pages — 26 different URLs under `/settings/*`
No canonical hub. `/settings` (`settings.py:78`) renders the env+kv override list; 25 sibling pages render their own forms (`/settings/api-tokens`, `/settings/app-aliases`, `/settings/app-capture-skip`, `/settings/app-icons`, `/settings/app-retention`, `/settings/app-groups`, `/settings/audio`, `/settings/blocklist`, `/settings/capture`, `/settings/dashboard`, `/settings/dashboard-widgets`, `/settings/digest-prompt`, `/settings/feed-tokens`, `/settings/focus-blocklist`, `/settings/keyboard`, `/settings/llm`, `/settings/ocr-languages`, `/settings/ocr-skip`, `/settings/phrase-tags`, `/settings/redaction`, `/settings/smtp`, `/settings/tag-aliases`, `/settings/theme`, `/settings/backup`).

**Recommendation:** `/settings` should become an *index* (group cards by category: Capture / Audio / Apps / OCR / LLM / Notifications / Sharing / Maintenance) that links to the existing sub-pages. The env-override grid is power-user only — move it to `/settings/advanced`.

### 1d. Search variants — six surfaces, three engines
| Route | File:line | Backend | Audience |
|---|---|---|---|
| `/search` | `search.py:103` | hybrid FTS + embeddings | main screenshot search |
| `/search/everything` | `corpus_search.py:47` | 5-table fan-out | combined corpus |
| `/search/word` | `word_search.py:157` | `ocr_word` table | per-word stats |
| `/notes/search` | `notes_search.py:108` | `notes_fts` | notes only |
| `/stickers/search` | `sticky_search.py:187` | `sticky_note` LIKE | stickies only |
| `/audio/search` | `audio_search.py:165` | audio_segment LIKE | transcripts |
| `/clipboard/semantic` | `clipboard_semantic.py:76` | embeddings | clipboard |

`corpus_search` already fans into the same five tables that the per-source pages cover (`shots / notes / annotations / stickies / clipboard`). `/audio/search` is the only artefact not in the fan-out.

**Recommendation:** keep `/search` (the primary), keep `/search/everything` (cross-corpus), add `audio_segment` as a 6th tab to `corpus_search.py`, then DELETE `/notes/search`, `/stickers/search`, `/audio/search` standalone pages (they become tabs of `/search/everything`). `/search/word` is a stats page misnamed as search — rename to `/stats/word-frequencies` to clarify. `/clipboard/semantic` is the only embedding-aware search — promote a `mode=semantic` flag to `/search` instead.

### 1e. Export variants — 16 separate routes, no index
Routers all sharing `prefix="/export"`: `app_shots_csv`, `archive_bundle`, `day_collage`, `ics_export`, `kanban_csv`, `monthly_stats_csv`, `ocr_txt_export`, `pdf_export`, `share_visits_csv`, `slack_summary`, `stats_csv`, `tag_ocr_export`, `weekly_pdf`, `words_csv`. Plus `/api/export/*` (`csv_export`, `export.py`, `full_export`, `journal_export`), and `/share/...`-prefixed PDF/ZIP for collections.

**Recommendation:** no consolidation needed inside the modules (each has its own renderer), but add an `/export` index page that lists all 16 with short descriptions — discoverability is the real bug. Also: `pdf_export.py:72 GET /export/pdf` and `weekly_pdf.py:83 GET /export/weekly-pdf` and `per_app_digest_pdf.py` all hand-roll PDF; consolidate the rendering helper, not the routes.

### 1f. Digest pages
- `/weekly` (`digest.py:17`), `/daily` + `/daily/{day}` (`daily_digests.py`), `/weekly-archive` + `/weekly-archive/{week_start}` (`weekly_digests.py`), `/digest/weekly-archive/{week_start}/card.png` (`digest_card.py`), `/digest/monthly` (`monthly_digests.py`), `/digest/monthly/.../card.png` (`monthly_digest_card.py`). Naming is inconsistent (`/weekly` vs `/weekly-archive`). Per-app digest is in its own router (`per_app_digest.py`).

**Recommendation:** rename `/weekly` → `/digest/weekly/latest` (or just `/digest/weekly`), drop the `-archive` suffix on the per-week pages. Consistent prefix tree: `/digest/{daily|weekly|monthly|per-app}[/{period}][.png]`.

### 1g. Share routes — three "share a shot" variants
- `share.py:80,94,115` — `/api/screenshots/{id}/share` + `/share/{token}` (legacy v0.x)
- `shot_share.py:190,219,265` — `/api/screenshot/{id}/share/create` + `/shot/share/{shot_id}/{token}` (newer)
- `shot_share_ui.py:39` — `/screenshot/{id}/share` (HTML form picker)
- `shot_embed.py:93` — `/screenshot/{id}/embed` (oEmbed)

Two parallel share-link schemas. `permalink` table coexists with both.

**Recommendation:** pick `shot_share.py` (newer, has revoke), migrate any live tokens, delete `share.py`. `shot_share_ui.py` becomes the front-end of `shot_share.py`. Keep `shot_embed.py` (different feature).

---

## 2. Worker proliferation

24 workers in `app/workers/`. Categorised:

**Live capture loops (3)** — `capture_loop.py`, `audio_worker.py`, `clipboard_worker.py`. Keep as-is.

**Backfill writers — periodic poll, no schedule (4)** — `hourly_card_worker.py`, `weekly_card_worker.py`, `daily_pin_worker.py`, `card_enrichment_worker.py`. All have identical shape: `_to_build()` → loop → write missing rows → sleep. Differ only in lookback / cadence / target table. **Consolidate** behind one generic `BackfillRunner(name, lookback, cadence, build_fn)` and register four instances. Saves ~300 LOC.

**Cron-style schedulers (8)** — `digest_scheduler.py`, `weekly_digest_scheduler.py`, `monthly_digest_scheduler.py`, `day_end_summary_scheduler.py`, `daily_email_scheduler.py`, `weekly_stats_email_scheduler.py`, `auto_backup_scheduler.py`. Every single one: 30-min poll, check `now.hour == settings.X_hour_local`, check idempotency marker, run job, set marker. **Consolidate** behind `ClockScheduler(name, hour, weekday|None, marker_kv, job_fn)`. Saves ~500 LOC and removes a constant source of "I changed the pattern in one and forgot the others" bugs.

**Background queues / processors (6)** — `ocr_worker.py` (701 LOC, complex), `embeddings_worker.py`, `inbox_worker.py`, `webhook_retry_worker.py`, `auto_translate_worker.py`, `tag_rule_worker.py`. Each has a real queue or watched directory; keep separate.

**Maintenance (2)** — `retention.py` (recycle + tier + budget), `audio_retention_worker.py`. **Consider merging** — both are nightly-ish purge workers; `audio_retention_worker` is 247 LOC of mostly boilerplate around a single SQL UPDATE. Could become a 30-line helper called from `retention.py`'s loop.

**Misc (1)** — `saved_search_alert.py`, `heartbeat.py`, `control.py`, `capture_runner.py`. Keep.

Total: **could cut 24 workers → ~15** with no feature loss.

---

## 3. Setting / kv_setting drift

Same logical setting in BOTH pydantic `Settings` AND `kv_settings` table. Whoever reads first wins, and the rules vary per call site — a real footgun.

| Setting | env field | kv key | Files |
|---|---|---|---|
| theme | `Settings.theme` (config.py:61) | `theme` | `theme.py:36`, `setup.py:88,128` |
| capture_interval_seconds | `Settings.capture_interval_seconds` (38) | `capture_interval_seconds`, `capture_interval_seconds_live` (TWO kv keys!) | `capture_loop.py:96`, `capture_settings.py:45`, `setup.py:89` |
| ocr_enabled | `Settings.ocr_enabled` (98) | `ocr_enabled` | `power_mode.py:38` |
| embeddings_enabled | `Settings.embeddings_enabled` (115) | `embeddings_enabled` | `power_mode.py:39` |
| byo_api_provider | `Settings.byo_api_provider` (102) | `byo_api_provider` (legacy) + `llm_provider` (new) | `llm/client.py:587,588`, `setup.py:90,130` |
| byo_api_key | `Settings.byo_api_key` (101) | `byo_<provider>_api_key` family + `byo_api_key` legacy | `llm/client.py:612,613` |
| tier_warm_after_days | `Settings.tier_warm_after_days` (53) | `tier_warm_after_days` | `setup.py:92,131` |
| tier_cold_after_days | `Settings.tier_cold_after_days` (54) | `tier_cold_after_days` | `setup.py:93,132` |
| retention_days | `Settings.retention_days` (42) | `retention_days` | `setup.py:94,133` |
| audio_capture_enabled | `Settings.audio_capture_enabled` (241) | `audio_capture_enabled` | `audio_settings.py:51,238` |
| audio_whisper_model | `Settings.audio_whisper_model` (222) | `audio_whisper_model` | `audio_settings.py:56,243` |
| audio_vad_threshold | `Settings.audio_vad_threshold` (249) | `audio_vad_threshold` | `audio_settings.py:54,241` |
| audio_retention_hot_days | `Settings.audio_retention_hot_days` (221) | `audio_retention_hot_days` | `audio_settings.py:55,242` |
| ui_language | (none) | `ui_language` | `auto_translate_worker.py:113`, `settings.py:93` |

Worst offender: `capture_interval_seconds` exists in **3 places** — env, kv `capture_interval_seconds`, AND kv `capture_interval_seconds_live` (the live-toggleable copy from v1.17). `capture_loop.py:96` reads the `_live` key only; `setup.py` writes the non-`_live` key only. Setup wizard silently has no effect on the running loop.

**Recommendation:** pick one model:
1. **kv-wins, env-is-bootstrap.** Every dual setting becomes "read kv if present, else fall back to env field". `get_settings()` exposes a method `effective_X()` that does the lookup, all readers switch to it. Codify in one place; setup-wizard / per-feature pages all write the kv row.
2. Drop the `_live` suffix convention entirely — collapse `capture_interval_seconds` / `capture_interval_seconds_live` into one.

Either way, document "kv wins" in `config.py` docstring so future ticks stop adding new env duplicates.

---

## 4. Half-finished / dead features

- **`blur_applied` table** — `migrations/021_blur_settings.sql`. Written at `ocr_worker.py:193` but never read anywhere (grep confirms zero `SELECT … blur_applied`). Migration costs nothing but the writes are dead weight.
- **`browser_tabs` table** — `migrations/007_browser_tabs.sql`. Only consumer is `companion.py` (browser extension). If extension is shipped and used, fine; if not, the writes accumulate. Worth confirming.
- **`process_app_remap` table** — `migrations/010`. Read only by `storage/process_remap.py`; UI surface is `process_remap.py` route — verify it's reachable from nav (it isn't in `base.html` more_items).
- **Two LLM-provider kv key families.** `_KV_LLM_PROVIDER = "llm_provider"` (new) and `_KV_LEGACY_PROVIDER = "byo_api_provider"` (legacy) both supported in `llm/client.py:30-34`. Was there ever a migration to drop the legacy reads?
- **`auto_translate_settings.py:14-19`** — module docstring says "the route deliberately does NOT register itself with the FastAPI app in main.py — wire it up in the route-coordinator instead". Check `app/web/main.py` actually does the `include_router` call; comments like this often mean half-wired routes.
- **`audio_settings.py:22-29`** — same self-disclaimer ("deliberately does NOT register itself"). Verify registration.
- **`feature_index.py`** — `/features` page. Auto-generates a route map. Good — but means every route file is reflected on the page, including the half-finished ones above, so users discover them.

---

## 5. Documentation / i18n gaps

`translations/en.json` and `ru.json` are 282 lines each, identical key set. Missing translation keys for new v1.14-v1.23 navigation pages:

| Page | URL | Hardcoded label | Missing key |
|---|---|---|---|
| Memory hub | `/memory` (`memory.py:29`) | `nav_memory` exists ✓ | covered |
| Memory weeks | `/memory/weeks` (`weekly_cards.py:22`) | — | no `nav_memory_weeks` / `title_memory_weeks` |
| Capture settings | `/settings/capture` (`capture_settings.py:70`) | hardcoded `"Захват"` at line 78 | should be `t("title_capture_settings")` |
| Quality lab | `/stats/quality-lab` (`quality_lab.py:136`) | — | no key |
| LLM cost | `/stats/llm-cost` (`llm_cost.py:138`) | — | no key |
| Activity | `/activity` (`activity_heatmap.py:72`) | — | no key |
| Shortcuts help | `/help/shortcuts` (`shortcuts_help.py:201`) | — | no key |
| Audio settings | `/settings/audio` | `title="Audio settings"` (line 273) — English only | no `title_audio_settings` |
| Blocklist | `/settings/blocklist` | hardcoded | no key |
| Theme | `/settings/theme` | `title="Theme"` (English) | no key |
| Backup | `/settings/backup/manage` | hardcoded | no key |

`base.html:143` also has a hardcoded Russian `"Ещё"` for the "More" dropdown — should be `t('nav_more')`. `base.html:191,208,221` carry more hardcoded Russian strings.

`capture_settings.py:78` is a literal RU string — Russian-by-default for the page, English users see Cyrillic.

**Recommendation:** add the ~12 missing keys to both `en.json` and `ru.json` in one pass; switch the hardcoded labels in routes/templates to `t(...)`. Then a CI grep for `title=\"[A-Z]` inside route handlers catches future drift.

---

## 6. Top 5 highest-leverage cleanups (ranked by ROI)

1. **Collapse Settings-vs-kv duplication into one resolver.** ~14 settings live in both places. One `get_effective(name)` helper + a single rule ("kv wins, env is the default") eliminates a whole class of "setup wizard didn't take effect" bugs. ~1 day; touches ~10 files but each touch is mechanical. Highest impact because it removes a footgun every new feature has to navigate.

2. **Generic `ClockScheduler` + `BackfillRunner` worker bases.** Replace 8 cron schedulers + 4 backfill writers (12 files, ~1600 LOC) with two helpers + 12 thin registrations (~400 LOC). Saves ~1200 LOC AND every future "weekly X" feature becomes a 30-line registration instead of a fresh 200-line file. ~1.5 days.

3. **Settings hub: turn `/settings` into a category index.** Currently 26 sibling pages exist with no discoverability except the address bar. Build a single grid template that groups the existing `/settings/*` routes by category. No backend changes; pure templating. ~half a day, immediate UX win.

4. **Kill the legacy `/share/{token}` codepath; standardise on `shot_share.py`.** Two parallel share-link tables/schemas is a security smell (token-revocation only lives on the new one). ~1 day including migration.

5. **Merge `/audio/search`, `/notes/search`, `/stickers/search` into `/search/everything` tabs; rename `/search/word` → `/stats/word-frequencies`.** The fan-out helper already covers 4 of the 5 sources — adding audio is ~20 lines. Removes 3 templates, 3 routes, and the user no longer has to remember which search box covers which corpus. ~half a day.

Honourable mentions: clean up the `_live` suffix kv convention (item 1 covers it), remove the dead `blur_applied` writes, add the 12 missing i18n keys (mechanical, ~1 hour but high visibility).

---

## Notes on this audit

- All file:line refs are absolute paths under `C:/www-Yaroslav/Persona/`.
- Route count: 274 `*.py` files in `app/web/routes/`. Most expose 1-3 endpoints, a few (`settings.py`, `ocr_admin.py`) expose more.
- The `_live` kv-suffix pattern (e.g. `capture_interval_seconds_live`) was introduced in v1.17 for live-toggleable settings; only one setting uses it consistently. Either commit to the convention or drop it.
- "v1.xx" markers in module docstrings are extremely helpful for this kind of audit — keep that habit.
