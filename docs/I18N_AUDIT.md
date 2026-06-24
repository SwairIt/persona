# Persona i18n audit — hardcoded English in templates

_Generated 2026-06-24. Scope: `app/web/templates/**/*.html`._

Persona is a Russian-language product. The translation system (`app/i18n.py` +
`app/web/templates_engine.py`) exposes a Jinja global `t('key')` backed by three
flat tables — `app/translations/{en,ru,de}.json` — with the fallback chain
`effective → ru → en → key` and default language `ru`. The three tables MUST keep
an identical key-set.

This audit catalogues **user-facing hardcoded English** that bypasses `t()` (so a
Russian user sees raw English), records what was converted in this pass, and
gives a directional inventory of the residual across the rest of the templates.

> Note: the bulk of Persona's templates already hardcode **Russian** copy (not via
> `t()`). That renders correctly for the default `ru` audience, so it is **not**
> the target of this pass. The target is hardcoded **English**, which shows up
> wrong for the Russian default. English-language `en` / `de` users still get
> Russian on those un-`t()`'d surfaces, but that is a separate, larger effort.

---

## Verification (run after the change)

```
$ .venv/Scripts/python.exe -c "import json,pathlib; b=pathlib.Path('app/translations'); \
  t={l:json.load(open(b/f'{l}.json',encoding='utf-8')) for l in ('en','ru','de')}; \
  print({l:len(x) for l,x in t.items()}); \
  assert set(t['en'])==set(t['ru'])==set(t['de']); print('PARITY OK')"
{'en': 376, 'ru': 376, 'de': 376}
PARITY OK

$ # jinja2.Environment.parse() on every changed template — no TemplateSyntaxError
PARSED OK base.html / dashboard.html / settings.html / settings_hub.html /
          auth_login.html / auth_signup.html / memory_graph.html / chat_index.html /
          advanced_settings.html / memory_settings.html

$ .venv/Scripts/python.exe -c "import app.web.main; print('IMPORT OK')"
IMPORT OK app.web.main
```

Key count: **303 → 376** (+73 new keys, added identically to all three tables).

> **Later pass (2026-06-24, +8 templates).** Key count grew to **885** across
> intervening passes, then **885 → 982** (+97 new keys) when the eight
> user-facing English templates below were localized. Parity verified
> (`set(en)==set(ru)==set(de)`, 982 each); all eight parse without
> `TemplateSyntaxError`; `import app.web.main` OK; no temp files in
> `app/translations/`.
>
> Converted: `summary.html`, `reading.html`, `reminders.html`,
> `favourites.html`, `focus.html`, `inbox.html`, `keywords.html`,
> `shared.html`. Each was wholly (or near-wholly) hardcoded English; now uses
> `{{ t('…') }}` with real ru/en/de. Numeric/id placeholders interpolate via
> the established `t('key').replace('{n}', x | string)` idiom (matching
> `dashboard.html` / `hours.html`). Reused existing keys where exact:
> `btn_add`, `btn_apply`, `btn_refresh`, `label_app`, `label_window`. Left as
> code/JS (not prose): `x-text` Alpine expressions, `<code>` env-var/path
> identifiers, the `--- title: … ---` front-matter literal.

> **Later pass (2026-06-24, +8 diagnostic/stats templates).** Key count grew
> **982 → 1201** (+219 new keys) when the eight user-facing English
> diagnostics/stats/admin templates below were localized. Parity verified
> (`set(en)==set(ru)==set(de)`, 1201 each); all eight parse without
> `TemplateSyntaxError` under the **app** Jinja environment (the bare
> `jinja2.Environment` chokes only on app-registered filters like `filesize`,
> not on syntax); every added `t()` key resolves in `ru.json`;
> `import app.web.main` OK; no temp files in `app/translations/`.
>
> Converted: `stats.html`, `storage_savings.html`, `retention_preview.html`,
> `retention_trend.html`, `idle_stats.html`, `embeddings_reindex.html`,
> `permalinks.html`, `lang_autodetect.html`. Reused existing keys where exact:
> `btn_apply`, `btn_settings`, `btn_prev`/`btn_today`/`btn_next`, `btn_go`,
> `label_app`, `label_window`, `label_grand_total`, `label_note`,
> `label_less`/`label_more`, `label_day_many`, `val_yes`/`val_no`,
> `msg_no_app_data`, `storage_report_back_to_stats`,
> `storage_report_th_date`/`_bar`/`_no_data`, `feed_tokens_th_created`, and
> the pre-existing `dow_mon`/`dow_wed`/`dow_fri` weekday keys. Localized
> user-facing JS string literals in `permalinks.html` (prompt + clipboard
> feedback) and the two `onsubmit="confirm(…)"` strings. Left as code (not
> prose): `<code>` env-var/path/HTTP-status identifiers, Unicode-script data
> codes (`cyrillic`/`latin`/`cjk`), JS state-machine status keys
> (`idle`/`running`/`done`/`error`) in `embeddings_reindex.html`, and the
> in-JS dynamic `Error:`/`Polling failed:` debug strings.

---

## Converted in this pass (high-visibility surfaces)

### `base.html` — navbar / drawer / footer (every page) — 4 strings, CONVERTED
| Original English | New key |
|---|---|
| `title="Worker heartbeat"` (status dot) | `tip_worker_heartbeat` |
| `📱 Mobile view` (mobile drawer link) | `nav_mobile_view` |
| `aria-label="Dismiss version banner"` | `aria_dismiss_version` |
| `title="Dismiss"` (version chip ×) | `btn_dismiss` |

Residual (left intentionally — Alpine `:title` bound JS template literals with live
numbers, low value, edit-risky): `:title="`${ocrPending} OCR pending`"`,
`:title="`${embPending} embeddings pending`"`, budget `:title`. The bare tokens
`OCR` / `AI` are kept (abbreviations).

### `dashboard.html` — main landing (`/now`) — 9 strings, CONVERTED
| Original English | New key(s) |
|---|---|
| `'day' / 'days'` (streak unit) | `label_day_one` / `label_day_many` |
| `title="3+ day streak"` | `tip_streak_3plus` |
| `generated <ts>` (latest digest) | `label_generated` |
| `alt="Pinned shot N"` | `alt_pinned_shot` |
| `no thumb` (empty thumb) | `msg_no_thumb` |
| `Live via SSE · refreshes every 2 s` | `msg_live_sse` |
| `title="Edit widgets"` | `tip_edit_widgets` |
| `edit` (widget link) | `label_edit_short` |
| `match / matches` (widget count unit) | `label_match_one` / `label_match_many` |

Residual: none (verified — zero Latin-script text nodes remain).

### `settings.html` — advanced kv/config page (`/settings`) — ~50 strings, CONVERTED
This page was almost entirely English. Converted in full:
- Intro line (`Read-only view of current config…`) → `settings_cfg_intro` (+ `…_tail`).
- **Capture** field labels: `cfg_interval`, `cfg_thumb_quality`, `cfg_thumb_max_width`,
  `cfg_dedup_hamming`, `cfg_retention`, `cfg_idle_threshold`.
- **OCR** labels + yes/no values + how-to: `cfg_enabled_in_env`, `cfg_binary_configured`,
  `cfg_binary_available`, `cfg_path`, `cfg_version`, `cfg_languages`, `val_yes`, `val_no`,
  `cfg_how_enable_ocr`, `cfg_install_tesseract`, `cfg_ubmannheim_build`, `cfg_set`,
  `cfg_restart_backfill`.
- **Paths / Server**: `cfg_data_dir`, `cfg_db`, `cfg_thumbnails`, `cfg_host`, `cfg_port`,
  `cfg_log_level`.
- **Language / Appearance** descriptions + checkbox labels: `cfg_lang_desc_a/_b`,
  `cfg_compact_desc`, `cfg_applied_via`, `cfg_attr_on_body`, `cfg_compact_label`,
  `cfg_grayscale_desc`, `cfg_grayscale_desc2`, `cfg_grayscale_label`,
  `cfg_reduce_motion_desc`, `cfg_reduce_motion_label`.
- **Capture behaviour** section: `cfg_capture_behaviour`, `cfg_capture_boot_a/_b/_c/_d`,
  `cfg_pause_on_boot_label` (the inline `<strong>Пауза</strong>` / `<em>Resume</em>`
  now reuse `status_paused` / `btn_resume`).
- **Digests / Notifications**: `cfg_antifomo_desc`, `cfg_antifomo_label`, `cfg_overriding`,
  `cfg_notif_desc_a/_b`.
- **Danger zone / overrides**: `cfg_danger_desc`, `cfg_irreversible`,
  `cfg_snapshots_unaffected`, `cfg_delete_by_app`, `cfg_confirm_delete_app`,
  `cfg_delete_by_range`, `cfg_confirm_delete_range`, `cfg_stored_in`,
  `cfg_overrides_desc_b`. (Two `onsubmit="confirm(...)"` JS strings localized too.)

Residual: none user-facing. Remaining Latin in-template is only `<code>` config
identifiers (`data-compact`, `kv_settings`, env var names, paths) which are code,
not prose.

---

## Priority templates already fully Russian (no English to convert)

These were inspected per the task brief and found to contain **no hardcoded
English** user-facing strings — their copy is already natural Russian (hardcoded,
not via `t()`, but correct for the default audience):

| Template | Surface | Status |
|---|---|---|
| `settings_hub.html` | `/settings/hub` — settings home | RU, no English |
| `advanced_settings.html` | `/settings/advanced` — chat feature flags | RU, no English |
| `memory_graph.html` | `/graph` — memory graph | RU, no English |
| `memory_settings.html` | `/settings/memory` — curated facts | RU, no English |
| `chat_index.html` | `/chat` — main chat UI (2281 lines) | RU; only `title="Persona"` (proper noun) |
| `auth_login.html`, `auth_signup.html` | login / signup | RU text (see note) |
| `auth_pending.html`, `auth_magic_sent.html`, `auth_set_password.html` | auth flows | RU, no English |

Note: `auth_login.html` / `auth_signup.html` carry `<html lang="en">` while the
visible copy is Russian — a wrong `lang` attribute (affects screen readers /
hyphenation), not user-facing text, so left for a separate fix. (`auth_pending`,
`auth_magic_sent`, `auth_set_password` already use `lang="ru"`.)

---

## Residual hardcoded English across the rest of the codebase (out of scope)

A Latin-script heuristic (`>Capitalised words<` in text nodes) flags ~574 hits
across ~194 templates. The heuristic **over-counts** (it also matches `<code>`
blocks, Alpine `x-text` JS, chart labels, and proper nouns like app names), so
treat these as an upper bound, not exact prose counts. The genuinely English,
user-facing residual is concentrated in **admin / diagnostic / stats / digest /
sharing** surfaces — power-user pages, not daily-visible — which the brief
explicitly excludes from this pass.

Top residual offenders (heuristic count) — recommended next batch, by surface:

- **Stats / analytics**: ~~`stats.html` (13)~~ **DONE (2026-06-24)**,
  `tag_stats.html` (9), `llm_usage.html` (9),
  `yearly_wrapped.html` (8), ~~`storage_savings.html` (10)~~ **DONE**,
  ~~`retention_trend.html` (4)~~ **DONE**, ~~`idle_stats.html` (5)~~ **DONE**,
  `sentiment_stats.html` (4). (Also `retention_preview.html` and
  `embeddings_reindex.html` — DONE.)
- **Sharing / embed**: `shot_share_ui.html` (12), `share_analytics.html` (7),
  ~~`permalinks.html` (3)~~ **DONE**.
- **Onboarding / misc**: `tour.html` (12), `setup_wizard.html` (3), `welcome.html` (1),
  `help.html` (6).
- **Diff / dedup / OCR admin**: `multi_day_diff.html` (10), `tag_merge_wizard.html` (10),
  `smart_dedup.html` (4), `ocr_admin.html` (4), `ocr_language_stats.html` (4),
  `day_ocr_diff.html` (4).
- **Admin / tokens / digests**: `demo_seeder.html` (8), `feed_tokens.html` (8),
  `api_tokens.html` (6), `agents_admin.html` (6), `app_icons_admin.html` (6),
  `digest_weekly.html` (7), `quality_lab.html` (7), ~~`lang_autodetect.html` (7)~~ **DONE (2026-06-24)**,
  `notes_csv_import.html` (7), `regex_rules.html` (6), `bulk_tag.html` (6),
  `quiet_hours.html` (6), ~~`retention_preview.html` (6)~~ **DONE**, ~~`summary.html` (6)~~ **DONE (2026-06-24)**,
  ~~`webhooks.html` (5)~~ **DONE**, `heartbeat_alerts.html` (5), ~~`embeddings_reindex.html` (5)~~ **DONE**,
  `facet_sets.html` (5), `outbox_admin.html` (5), `bookmarklet.html` (5),
  `capture_weekly_trend.html` (5), `tag_merge.html` (5).

…plus a long tail of templates with 1–4 hits each (most of which are `<code>`
identifiers, `x-text` expressions, or app/brand names — i.e. false positives).

### Done in the 2026-06-24 +8 pass (user-facing English → `t()`)

These eight were wholly/near-wholly hardcoded English and are now fully
localized (ru/en/de), so they are **no longer residual**:

| Template | Surface |
|---|---|
| `summary.html` | `/summary` — daily LLM summary |
| `reading.html` | `/reading` — read-later list |
| `reminders.html` | `/reminders` — per-day todos |
| `favourites.html` | `/favourites` — starred shots |
| `focus.html` | `/focus` — Pomodoro timer |
| `inbox.html` | `/inbox` — Markdown drop-folder import |
| `keywords.html` | `/keywords` — top-keyword cloud |
| `shared.html` | `/share/<token>` — public shared screenshot |

### Done in the 2026-06-24 +8 diagnostics/stats pass (user-facing English → `t()`)

These eight (reachable from the settings hub Diagnostics card or linked off
`/stats`) were wholly/near-wholly hardcoded English and are now fully localized
(ru/en/de), so they are **no longer residual**:

| Template | Surface |
|---|---|
| `stats.html` | `/stats` — overall stats (hub → «Общая статистика») |
| `storage_savings.html` | `/storage-savings` — bytes reclaimed (linked off `/stats`) |
| `retention_preview.html` | `/retention-preview` — dry-run retention sweep |
| `retention_trend.html` | `/stats/retention-trend` — daily demote/delete trend |
| `idle_stats.html` | `/idle` — per-day active vs. idle (AFK) |
| `embeddings_reindex.html` | `/admin/embeddings-reindex` — bulk vector re-index |
| `permalinks.html` | `/permalinks` — short `/go/<slug>` redirects |
| `lang_autodetect.html` | `/admin/ocr-lang-autodetect` — per-app OCR language recs |

### Suggested follow-up order (if continuing)
1. `tag_stats.html` / `llm_usage.html` / `yearly_wrapped.html` (remaining stats).
2. `welcome.html` / `setup_wizard.html` / `tour.html` (first-run onboarding).
3. `shot_share_ui.html` (public-facing share surface).
4. The OCR / diff / dedup admin cluster.
5. The token / digest admin cluster (lowest visibility).
