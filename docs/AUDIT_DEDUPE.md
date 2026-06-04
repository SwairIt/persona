# Persona route consolidation audit

Read-only audit of `app/web/routes/` (250 files, 249 `include_router` calls) and `app/web/templates/` (193 .html). Goal: merge near-clones, kill cruft, leave behaviour unchanged.

Baselines used throughout:
- All path/LOC counts come from `wc -l` against the working tree at HEAD (2026-06-04).
- "Reachable from template" was checked by grepping `app/web/templates/` for the route's literal URL prefix.
- Every route file currently lives in `app/web/routes/`, imports cleanly, and is registered in `app/web/main.py` (lines 21–271 for imports, 329–577 for `include_router`).

---

## 1. Search routes — collapse to one

| File | LOC | Verb / Path | Searches | Unique angle |
|---|---:|---|---|---|
| `search.py` | 499 | `GET /search`, `GET /api/search-history`, `POST /api/search-history/clear` | Shots (FTS5 + semantic hybrid) | Facets, sort, tier, tag, size, script filters. The flagship. |
| `audio_search.py` | 210 | `GET /audio/search`, `GET /api/audio/search.json` | `audio_transcript` table | Plain LIKE; HTML+JSON twin. |
| `notes_search.py` | 150 | `GET /notes/search`, `GET /api/notes/search.json` | `screenshot_notes` (FTS) | HTML+JSON twin. |
| `sticky_search.py` | 210 | `GET /stickers/search` | `sticky_note` body LIKE | HTML only. |
| `word_search.py` | 193 | `GET /search/word` | `ocr_words` rows, `min_conf` cut-off | Per-word, confidence-aware. |
| `corpus_search.py` | 84 | `GET /search/everything` | All of the above via `app.corpus_search` | Single aggregator page — most ambitious. |
| `search_autocomplete.py` | 126 | `GET /api/search/autocomplete` | Title/app suggestions | JSON only. |
| `search_facets.py` | 97 | `GET /api/search/facets.json` | Tag/app/date histograms for the search page | Used by `search.py` template. |
| `search_query_stats.py` | 89 | `GET /stats/search-queries` | `search_history` aggregates | Stats page, not a search. |
| `search_tag_all.py` | 309 | `POST /api/search/tag-all` | Bulk-tags every hit | Mutation, not a search. |
| `saved_searches.py` | 154 | `GET/POST /searches`, `POST /searches/{slug}/delete`, `GET /searches/{slug}` | CRUD over saved query slugs | Distinct concern. |
| `semantic_similar.py` | 80 | `GET /shot/{id}/similar` | "More like this" embeddings | Per-shot, not a free-text search. |

**Proposal.**
- **Keep separate** because the path or template diverges meaningfully:
  - `search.py` (main) — already canonical.
  - `saved_searches.py` — CRUD, not a search; stays.
  - `search_tag_all.py` — bulk mutation; stays.
  - `semantic_similar.py` — different UX (related-to-shot, no query box).
  - `search_query_stats.py` — really a stats page; move under stats grouping (see §6).
  - `search_autocomplete.py` + `search_facets.py` — JSON helpers for the search page, ~220 LOC combined. Tiny. Leave as is; merging them into `search.py` bloats the flagship to 700 LOC with no gain.
- **Merge into one parameterised endpoint** `GET /search?kind={shots|notes|stickers|audio|words|everything}`:
  - `audio_search.py`, `notes_search.py`, `sticky_search.py`, `word_search.py`, `corpus_search.py` → folded into a single dispatcher inside `search.py`. Each `kind` keeps its own `_run_search` helper (already small and self-contained) and a small per-kind template fragment.
  - JSON twins collapse to `GET /api/search.json?kind=…` — the four current `*.json` endpoints all return the same `{query, results, total}` shape, so one handler covers them.
  - Path aliases (`/audio/search`, `/notes/search`, etc.) stay as `RedirectResponse(307, "/search?kind=audio&q=…")` shims for one release, then go away. The dispatcher in `search.py` keeps the per-kind template name list explicit so a missing template fails loud at boot, not at request time.

**Saved LOC after merge:** ~700 LOC (sum of audio/notes/sticky/word/corpus minus a ~100 LOC dispatcher).

---

## 2. CSV / export routes — collapse

| File | LOC | Route | Exports | Format |
|---|---:|---|---|---|
| `export.py` | 105 | `GET /day`, `GET /range` | screenshots + notes for date range | JSON |
| `csv_export.py` | 107 | `GET /search.csv`, `GET /search.md` | last search results | CSV + Markdown |
| `full_export.py` | 71 | `GET /full.zip` | full DB + thumbs | ZIP |
| `journal_export.py` | 150 | `GET /journal.md` | notes as Markdown journal | Markdown |
| `pdf_export.py` | 119 | `GET /pdf` | one day → PDF | PDF |
| `weekly_pdf.py` | 127 | `GET /weekly-pdf` | one week → PDF | PDF |
| `ics_export.py` | 62 | `GET /calendar.ics` | shots → calendar events | ICS |
| `ocr_txt_export.py` | 102 | `GET /ocr.txt` | OCR full-text dump | text |
| `sticky_export.py` | 74 | `GET /export/sticky-notes.json` | sticky_note table | JSON |
| `tag_ocr_export.py` | 264 | `GET /tag/{tag}/ocr.txt` | OCR for one tag | text |
| `stats_csv.py` | 66 | `GET /stats.csv` | stats rollup | CSV |
| `monthly_stats_csv.py` | 72 | `GET /monthly-stats.csv` | monthly rollup | CSV |
| `share_visits_csv.py` | 137 | `GET /share-visits.csv` | share-link visits | CSV |
| `kanban_csv.py` | 241 | `GET /kanban.csv` | day-kanban dump | CSV |
| `words_csv.py` | 200 | `GET /words.csv` | OCR word frequencies | CSV |
| `annotations_csv.py` | 139 | `GET /export/annotations.csv` | annotations | CSV |
| `app_shots_csv.py` | 174 | `GET /app/{app_name}/shots.csv` | per-app shot list | CSV |
| `share_collection_pdf.py` | 143 | shared collection → PDF | PDF |
| `notes_csv_import.py` | 184 | `GET/POST /admin/notes-csv-import` | **import**, not export |

**Total exports surface:** 2537 LOC across 19 files.

**Proposal.** Introduce one router `app/web/routes/export_v2.py` with a single signature:

```
GET /export/{kind}.{format}
  kinds:   day, range, search, journal, ocr, sticky, tag-ocr, stats, monthly-stats,
           share-visits, kanban, words, annotations, app-shots, full,
           daily-pdf, weekly-pdf, calendar
  formats: csv | md | json | txt | ics | pdf | zip
```

- Each (kind, format) pair maps to one renderer function. Renderers stay in their existing module — only the route registration collapses. So `csv_export.py` keeps `_render_search_csv()` but loses its `@router.get`.
- `notes_csv_import.py` is **not** an export — leave it alone, just rename for clarity (`notes_csv_io.py`).
- Keep the legacy URLs as 307 redirects to the new `/export/...` path for one release.

**Saved LOC after merge:** ~600 LOC of route boilerplate and duplicated `Content-Disposition` / streaming setup. The bulk (CSV writers, PDF builders) stays — that's actual logic.

---

## 3. OCR routes — keep the core, kill the cruft

23 OCR route files, 3120 LOC total. Every URL is referenced from at least one template (verified by grepping `templates/` for the route's URL prefix) **except** the ones marked DELETE/MERGE below.

| File | LOC | Routes | Template ref? | Verdict |
|---|---:|---|---|---|
| `ocr_status.py` | 42 | `GET /status` | implicit (health) | **KEEP** — generic status JSON, used by external pings. |
| `ocr_admin.py` | 76 | `GET /ocr-admin`, `POST /ocr-admin/reset-*` (3) | `ocr_admin.html` + cross-linked from 5 OCR templates | **KEEP** — the OCR hub. |
| `ocr_skip.py` | 60 | `GET/POST /settings/ocr-skip` | `ocr_skip.html`, linked from `app_capture_skip.html` | **KEEP** — distinct settings page. |
| `ocr_languages.py` | 62 | `GET/POST /settings/ocr-languages` | `ocr_languages.html`, linked from `setup_wizard.html` and `lang_autodetect.html` | **KEEP** — setup-critical. |
| `ocr_phrase_tags.py` | 50 | `GET/POST /settings/phrase-tags`, delete | own template `phrase_tags.html` | **KEEP** — distinct settings. |
| `ocr_overlay.py` | 95 | `GET /screenshot/{id}/overlay`, `/words.json` | `ocr_overlay.html`, used by shot viewer | **KEEP** — per-shot viewer. |
| `ocr_diff.py` | 72 | `GET /diff/ocr/{a}/{b}` | linked from `ocr_near_dup.html` | **KEEP** — paired with near-dup workflow. |
| `ocr_history.py` | 115 | `GET /api/screenshot/{id}/ocr-history.json`, `POST /api/ocr-history/{id}/revert` | called from `screenshot.html` JS | **KEEP** — backs the editor revert button. |
| `ocr_edit.py` | 178 | `POST /api/screenshot/{id}/ocr` | called from `screenshot.html` (`ocr_edit.js`) | **KEEP** — inline edit save endpoint. |
| `ocr_retry.py` | 179 | `GET /admin/ocr-retry`, 2× `POST` | `ocr_retry.html`, cross-linked from `ocr_rerun_n.html` | **KEEP** — retry queue UI. |
| `ocr_rerun_n.py` | 162 | `GET/POST /admin/ocr-rerun-n` | `ocr_rerun_n.html`, linked from `ocr_vision_replace.html` | **MERGE → `ocr_retry.py`** — both are "rerun a batch" admin pages. Combine into `/admin/ocr-retry?mode=last-n`. ~150 LOC saved. |
| `ocr_near_dup.py` | 122 | `GET /admin/ocr-near-duplicates`, `POST .../delete` | `ocr_near_dup.html` | **KEEP** but see §5 — fold into dedup family. |
| `ocr_find_replace.py` | 256 | `GET/POST /admin/ocr-find-replace[/preview\|/apply]` | `ocr_find_replace.html`, linked from 2 OCR pages | **KEEP** — distinct bulk-edit tool. |
| `ocr_vision.py` | 110 | `POST /api/screenshot/{id}/ocr-vision`, `GET /admin/ocr-vision` | `ocr_vision_admin.html` | **KEEP** — vision-model rerun. |
| `ocr_vision_replace.py` | 172 | `GET /admin/ocr-vision-replace`, `POST .../apply` | `ocr_vision_replace.html` | **KEEP** — distinct apply workflow. |
| `ocr_translate.py` | 183 | `POST /api/screenshot/{id}/ocr-translate`, `GET /admin/ocr-translate` | no template ref to `/admin/ocr-translate` found; admin page only reachable via `feature_index` | **KEEP** but flag — admin page is orphan from main nav; either link it from `ocr_admin.html` or accept it is feature-index-only. |
| `ocr_words_tsv.py` | 149 | `GET /ocr-words/{id}.tsv` | no template ref found | **DELETE** — TSV dump per shot; never linked anywhere; same data is exposed by `ocr_overlay.py`'s `/words.json`. 149 LOC. |
| `ocr_emails.py` | 68 | `GET /api/screenshot/{id}/emails.json`, `GET /stats/emails` | no template ref to either URL; `stats/emails` template `emails_stats.html` exists but no link to it from anywhere | **MERGE → `ocr_phones.py`** + park behind `/ocr-extracts/{kind}` (see below). |
| `ocr_phones.py` | 68 | `GET /api/screenshot/{id}/phones.json`, `GET /stats/phones` | no template ref | **MERGE → `ocr_emails.py` into one file `ocr_extracts.py`** — they are byte-for-byte symmetric (same shape, swap "email"↔"phone"). Saves ~50 LOC. |
| `ocr_error_rate.py` | 288 | `GET /stats/ocr-error-rate`, `GET /api/ocr-error-rate.json` | `ocr_error_rate.html` (own) | **KEEP** — but see §6, this is a stats page. |
| `ocr_length_chart.py` | 369 | `GET /stats/ocr-length`, `GET /api/ocr-length.json` | `ocr_length_chart.html` (own) | **KEEP** — stats; see §6. |
| `ocr_language_stats.py` | 142 | `GET /stats/ocr-languages`, `GET /api/ocr-languages.json` | `ocr_language_stats.html` (own) + linked from `lang_autodetect.html` | **KEEP** — stats; see §6. |
| `ocr_txt_export.py` | 102 | `GET /ocr.txt` | no template ref | **MERGE → exports (§2)**. |

**Net OCR action:**
- DELETE: `ocr_words_tsv.py` (149 LOC).
- MERGE: `ocr_rerun_n.py` → `ocr_retry.py` (~150 LOC saved); `ocr_emails.py`+`ocr_phones.py` → `ocr_extracts.py` (~50 LOC saved); `ocr_txt_export.py` → §2.
- Everything else KEEP — they have distinct UI surface, distinct DB writes, or distinct admin workflows.

---

## 4. Digest routes — share the engine

| File | LOC | Routes | Period / Source |
|---|---:|---|---|
| `digest.py` | 87 | `GET /digest/weekly` | **No-LLM** week recap from `screenshots` table |
| `daily_digests.py` | 59 | `GET /digest/daily`, `GET /digest/daily/{day}` | LLM, `daily_digest` table |
| `weekly_digests.py` | 77 | `GET /digest/weekly-archive`, `GET /digest/weekly-archive/{week}` | LLM, `weekly_digest` table |
| `monthly_digests.py` | 79 | `GET /digest/monthly-archive`, `GET /digest/monthly-archive/{month}` | LLM, `monthly_digest` table |
| `digest_card.py` | 155 | `GET /weekly-archive/{week}/card.png` | PNG card for weekly |
| `monthly_digest_card.py` | 172 | `GET /monthly-archive/{month}/card.png` | PNG card for monthly |
| `weekly_stats_card.py` | 169 | `GET /weekly-card.png` | PNG card from `digest.py`'s stats |
| `digest_prompts.py` | 208 | `GET/POST /settings/digest-prompt`, monthly variant, resets (5 routes) | Prompt CRUD |
| `per_app_digest.py` | 211 | `GET /digest/apps`, `GET /api/per-app-digest.json`, `POST` | Per-app rollup |
| `day_tldr.py` | 79 | `GET /api/day-tldr.json`, `POST /api/day-tldr/{day}/regenerate` | One-day TL;DR |

**Diagnosis.** `daily_digests.py`, `weekly_digests.py`, `monthly_digests.py` are near-byte-for-byte clones (each is index + detail of one table by one date key). Same with the two PNG card routes. The settings-prompt module is one file with separate daily/weekly/monthly handlers — already shared.

**Proposal.** One thin engine + period adapters:

```
app/digest_engine.py
  class DigestPeriod(period_key: str, table: str, key_col: str, ...):
      async fetch_index(conn, limit) -> list[dict]
      async fetch_one(conn, key)    -> dict | None
      async render_card(...)         -> bytes

PERIODS = {
    "daily":   DigestPeriod("daily",   "daily_digest",   "day",        ...),
    "weekly":  DigestPeriod("weekly",  "weekly_digest",  "week_start", ...),
    "monthly": DigestPeriod("monthly", "monthly_digest", "month",      ...),
}
```

Then one route file `digest_archive.py` exposes:

```
GET /digest/{period}-archive
GET /digest/{period}-archive/{key}
GET /digest/{period}-archive/{key}/card.png
```

This collapses `daily_digests.py + weekly_digests.py + monthly_digests.py + digest_card.py + monthly_digest_card.py` (542 LOC) into ~250 LOC of engine + ~150 LOC of one route file. **~150 LOC saved**.

**Keep separate** because they are not period-symmetric:
- `digest.py` (`/digest/weekly` — no-LLM stats recap) — completely different data path. Rename to `weekly_recap.py` so it stops conflicting with the LLM archive in `/digest/weekly-archive`.
- `digest_prompts.py` — prompt CRUD.
- `per_app_digest.py` — sliced by app, not by period.
- `day_tldr.py` — JSON-only, per-day; nothing in common with the archive UI.
- `weekly_stats_card.py` — its card image is built from `digest.py`'s stats, not from a `*_digest` table.

---

## 5. Dedup routes

| File | LOC | Routes | What it does |
|---|---:|---|---|
| `ocr_near_dup.py` | 122 | `GET /admin/ocr-near-duplicates`, `POST .../delete` | Lists pairs of shots whose OCR text is near-identical (Levenshtein/shingle). Bulk-delete one side. |
| `dedup_cluster.py` | 456 | `GET /admin/dedup-clusters`, `POST .../{id}/split`, `POST .../{id}/merge` | Lists pHash dedup clusters from `dedup_groups`. Split/merge cluster membership. |
| `thumb_dedup.py` | 46 | `GET /admin/thumb-dedup`, `POST .../scan` | Runs `app.thumb_dedup.scan_and_dedup` (on-disk thumbnail file dedup). |
| `dup_suggest.py` | 80 | `GET /api/dup-suggest/{id}` | Returns "likely duplicates" strip for one shot; fragment template `_dup_suggest_strip.html`. |

**Proposal.** These three pages target **three different signals** (OCR similarity, pHash clusters, on-disk thumb files) and the operator needs to act on each separately. They are not duplicates of each other — keep all four. But:

- **Unify URL prefix** under `/admin/dedup/...`:
  - `/admin/dedup/ocr-pairs` (was `/admin/ocr-near-duplicates`)
  - `/admin/dedup/clusters` (was `/admin/dedup-clusters`)
  - `/admin/dedup/thumbs` (was `/admin/thumb-dedup`)
- **Share one nav landing page** (`/admin/dedup` index) so the operator finds all three without `feature_index`.
- **`dup_suggest.py` is a fragment helper, not a dedup page** — keep separate, but rename URL `/api/dedup/suggest/{id}` for consistency.

No LOC saved, but the operator experience stops being a scavenger hunt across three URLs.

---

## 6. Stats routes

| File | LOC | Route | Subject |
|---|---:|---|---|
| `stats.py` | 179 | `GET /stats`, `GET /stats.json` | Global rollup |
| `app_stats.py` | 113 | `GET /apps`, `GET /apps/{name}` | Per-app shot list |
| `idle_stats.py` | 126 | `GET /idle`, `GET /api/idle.json` | Idle-time distribution |
| `embeddings_stats.py` | 98 | `GET /stats/embeddings`, `GET /api/embeddings-stats.json` | Embeddings coverage |
| `ocr_language_stats.py` | 142 | `GET /stats/ocr-languages`, `GET /api/ocr-languages.json` | OCR script histogram |
| `ocr_error_rate.py` | 288 | `GET /stats/ocr-error-rate`, `GET /api/ocr-error-rate.json` | OCR-confidence error rate over time |
| `ocr_length_chart.py` | 369 | `GET /stats/ocr-length`, `GET /api/ocr-length.json` | OCR text length per day |
| `sentiment_stats.py` | 411 | `GET /stats/sentiment`, `GET /api/sentiment.json` | Sentiment timeline |
| `search_query_stats.py` | 89 | `GET /stats/search-queries` | Top queries |
| `collection_visit_stats.py` | 246 | `GET /admin/collection-visits`, `GET /api/collection-visits.json` | **BROKEN** — template `collection_visit_stats.html` missing (see §8) |
| `audio_stats.py` | 253 | `GET /stats/audio`, `GET /api/audio-stats.json` | Audio capture stats |
| `stats_csv.py` | 66 | `GET /stats.csv` | CSV — see §2 |
| `monthly_stats_csv.py` | 72 | `GET /monthly-stats.csv` | CSV — see §2 |

**Proposal.** Three sub-groupings:

1. **OCR sub-stats** (`ocr_language_stats.py`, `ocr_error_rate.py`, `ocr_length_chart.py`) — same URL family `/stats/ocr-*`, same shape (page + `.json` twin), same window-switcher pattern. The templates already cross-reference each other. Build one `app/stats/ocr/` package and have one route file `ocr_stats.py` dispatch on `/stats/ocr-{metric}`. **~250 LOC saved** of duplicated query-window plumbing.

2. **Top-level rollup** stays in `stats.py`. Move `search_query_stats.py` (89 LOC) into it as `GET /stats/search-queries` is already a logical sub-tab of stats.

3. **Single-purpose stats pages** that don't fit a family (`sentiment_stats`, `embeddings_stats`, `audio_stats`, `idle_stats`, `app_stats`, `collection_visit_stats`) — leave them alone, they all have non-trivial visualisations.

CSV variants → §2.

---

## 7. Orphan modules

Programmatic check (parsed `app/web/main.py` imports + `include_router` calls vs `os.listdir`):

- **Files in `app/web/routes/` not imported in `main.py`:** `setup_gate.py` only. That file is middleware, imported separately at line 20 (`from app.web.routes.setup_gate import SetupGateMiddleware`). **Not an orphan.**
- **Imported but never `include_router`'d:** none.
- **`include_router` calls without matching import:** none.

**Result: zero orphan route modules.** The 250 files are all wired up. The bloat is duplication, not dead code.

---

## 8. Templates the routes need

Programmatic check (template references collected from both `routes/*.py` and `templates/*.html`, compared against `os.walk(templates/)`):

- **Total `.html` files:** 193
- **Templates referenced nowhere:** **0** (every template in the tree is referenced by either a route or another template).
- **Routes pointing at a missing template — broken route:**

| Route file | Route URL | Missing template |
|---|---|---|
| `app/web/routes/collection_visit_stats.py:207` | `GET /admin/collection-visits` | `collection_visit_stats.html` |

The 500 you would get hitting `/admin/collection-visits` is a real, currently-shipping bug. The JSON sibling `/api/collection-visits.json` works fine because it doesn't render a template.

**Fix:** either ship the template or remove the page route and keep the JSON endpoint.

---

## 9. Concrete kill list (one-PR, zero-behaviour-loss)

Conservative. Only files that are duplicate, broken, or genuinely unused.

| File | LOC | Why |
|---|---:|---|
| `app/web/routes/ocr_words_tsv.py` | 149 | `/ocr-words/{id}.tsv` referenced from no template; the same word-with-confidence data is already served by `ocr_overlay.py`'s `/api/screenshot/{id}/words.json`. |
| **broken route stub** in `app/web/routes/collection_visit_stats.py` (lines 186–218, the HTML page handler only) | ~35 | Page handler points at non-existent `collection_visit_stats.html`. Keep `_compute` and the `/api/collection-visits.json` handler; drop the dead HTML handler until the template ships. |

Total: ~184 LOC.

(Deliberately not putting `ocr_emails.py`, `ocr_phones.py`, etc. on the kill list — they're working features, just under-linked. They belong in §10 merges.)

---

## 10. Concrete merge list

| Sources | → Target | Approx LOC saved |
|---|---|---:|
| `audio_search.py` (210) + `notes_search.py` (150) + `sticky_search.py` (210) + `word_search.py` (193) + `corpus_search.py` (84) | `search.py` with `?kind=` dispatcher | ~700 |
| `csv_export.py` + `full_export.py` + `journal_export.py` + `pdf_export.py` + `ics_export.py` + `sticky_export.py` + `tag_ocr_export.py` + `stats_csv.py` + `monthly_stats_csv.py` + `share_visits_csv.py` + `kanban_csv.py` + `words_csv.py` + `annotations_csv.py` + `app_shots_csv.py` + `weekly_pdf.py` + `share_collection_pdf.py` + `ocr_txt_export.py` (sum ≈ 2 290) | one `export_v2.py` with `/export/{kind}.{format}`; renderer functions stay in their existing modules but lose their `@router.get` | ~600 (route boilerplate + `Content-Disposition` repetition) |
| `daily_digests.py` (59) + `weekly_digests.py` (77) + `monthly_digests.py` (79) + `digest_card.py` (155) + `monthly_digest_card.py` (172) | one `digest_archive.py` + `app/digest_engine.py` (period adapters) | ~150 |
| `ocr_rerun_n.py` (162) | `ocr_retry.py` with `?mode=last-n` | ~150 |
| `ocr_emails.py` (68) + `ocr_phones.py` (68) | `ocr_extracts.py` parameterised by kind | ~50 |
| `ocr_language_stats.py` (142) + `ocr_error_rate.py` (288) + `ocr_length_chart.py` (369) | `ocr_stats.py` (`/stats/ocr-{metric}`) sharing the window-switcher / JSON-twin scaffolding | ~250 |
| `search_query_stats.py` (89) | `stats.py` as a sub-tab | ~50 |

Plus straight kills from §9: **184 LOC**, **1 file removed**.

---

## Summary

Counts:
- Files in `app/web/routes/` today: **250** (+ `setup_gate.py` middleware).
- Routes wired up in `main.py`: **249**.
- Files involved in merges/kills above: **42** (collapse to ~9 target files).
- Files unchanged: **208**.
- Currently broken: **1** route URL (`/admin/collection-visits` — missing template).
- Unused templates: **0**.

**Estimated LOC reduction: ~2 130 LOC (250 → ~217 files, -13% LOC against ~16 200 LOC currently in `routes/`).**

The bulk of the cleanup is parametrising the search dispatcher (§1) and the export dispatcher (§2). Everything else is incremental.
