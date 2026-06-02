# Persona v0 — Implementation Spec

This is the source of truth for what v0 must be. The autonomous loop reads this every iteration.

## Goal

A working personal-memory app that the developer can install on their own Windows machine, run for a week, and use to find things from their own past — "show me what I was reading at 14:23 yesterday", "find the screen where I was looking at SQLAlchemy docs", "list the apps I used most this week".

## Out of scope for v0

- Cloud sync. Single device only.
- Mobile. Desktop only.
- Ambient audio recording (legal landmine — Microsoft Recall lesson).
- Zero-knowledge encryption (conflicts with optional cloud LLM — postpone to v1).
- Semantic search via embeddings (skip ML deps tonight — postpone to v0.2, FTS5 covers v0).
- Multi-monitor support beyond "primary monitor only" — handle in v0.1.
- macOS / Linux ports — Windows-only tonight.
- Code signing / installer — manual `uv run` for v0.

## Architecture

```
+--------------------------------------------------+
|                  Web UI (browser)                 |
|   timeline · search · settings · stats            |
+----------------------+---------------------------+
                       | HTMX over HTTP
+----------------------v---------------------------+
|              FastAPI (app/web)                    |
|   routes: /, /search, /screenshot/{id},           |
|   /settings, /stats, /api/capture/*               |
+----------------+----------------+-----------------+
                 |                |
  +--------------v---+   +--------v---------+
  |  Capture worker  |   |  OCR worker      |
  |  (asyncio task)  |   |  (asyncio task)  |
  |  every 5s        |   |  drains queue    |
  +--------+---------+   +--------+---------+
           |                      |
  +--------v----------------------v---------+
  |        SQLite (FTS5) + filesystem        |
  |  data/persona.db                         |
  |  data/thumbnails/YYYY-MM-DD/<id>.webp    |
  +------------------------------------------+
```

## Module responsibilities

### `app/settings/config.py`
Pydantic-settings `Settings` class reading from `.env`. Single global instance returned by `get_settings()`. Fields: `data_dir`, `db_path`, `thumbnails_dir`, `capture_interval_seconds`, `thumbnail_quality`, `thumbnail_max_width`, `dedup_hamming_threshold`, `retention_days`, `idle_threshold_seconds`, `host`, `port`, `log_level`, `tesseract_path` (Optional[Path]), `tesseract_langs`, `ocr_enabled`. All paths must be resolved absolute on init.

### `app/capture/screen.py`
Sync function `capture_primary_monitor() -> CaptureResult` returning `(image: Pillow.Image.Image, width: int, height: int, captured_at: datetime)`. Uses `mss` under the hood. No threading here — pure function.

### `app/capture/window.py`
Sync function `get_active_window() -> ActiveWindow | None` returning `(title: str, app_name: str, process_name: str)`. Uses `pygetwindow` for title + `psutil` for process name via foreground PID. Returns `None` when no foreground window (locked screen).

### `app/capture/idle.py`
Sync function `seconds_since_last_input() -> float`. On Windows uses `GetLastInputInfo` via `ctypes`. Used to pause capture when user is idle.

### `app/storage/db.py`
Async helpers using `aiosqlite`. Functions: `get_connection()`, `init_database(db_path)` — runs `schema.sql`, `migrate(db_path)` — runs `migrations/*.sql` in order. Connection pragmas: `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`.

### `app/storage/schema.sql`
Tables:
- `screenshots` — one row per captured frame: `id INTEGER PK`, `captured_at TIMESTAMP`, `monitor_index INT`, `width INT`, `height INT`, `thumbnail_path TEXT`, `phash TEXT`, `app_name TEXT`, `window_title TEXT`, `process_name TEXT`, `ocr_status TEXT CHECK in ('pending', 'done', 'skipped', 'failed')`, `ocr_text TEXT`, `dedup_group_id INTEGER NULL FK`, `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP`.
- `dedup_groups` — represents "we have seen this near-duplicate screen N times": `id INTEGER PK`, `representative_screenshot_id INT FK`, `phash TEXT UNIQUE`, `seen_count INT DEFAULT 1`, `first_seen TIMESTAMP`, `last_seen TIMESTAMP`.
- `capture_events` — log of capture-loop heartbeats / pauses / errors for debugging stats: `id`, `ts`, `event_type` (one of `start`, `pause`, `resume`, `error`, `heartbeat`), `details JSON`.
- `screenshots_fts` — FTS5 virtual table over `(ocr_text, window_title, app_name)` with `content='screenshots'`, `content_rowid='id'`. Triggers `screenshots_ai/au/ad` to keep FTS in sync.

### `app/storage/thumbnails.py`
`save_thumbnail(image, captured_at, screenshot_id) -> Path`. Writes WebP q=`settings.thumbnail_quality`, resizes to width `settings.thumbnail_max_width` preserving aspect ratio. Path: `data/thumbnails/YYYY-MM-DD/<screenshot_id>.webp`.

### `app/dedup/phash.py`
`compute_phash(image) -> str` — returns hex string from `imagehash.phash`. `is_near_duplicate(p1, p2, threshold)` — hamming distance under threshold. `find_or_create_dedup_group(conn, phash, screenshot_id, threshold)` — returns existing group id if hamming distance to any group's representative phash is ≤ threshold; else inserts new group. Increments `seen_count` and bumps `last_seen`.

### `app/ocr/tesseract.py`
`is_available(tesseract_path) -> bool`. `extract_text(image, langs, tesseract_path) -> str`. If `tesseract_path` is None or invalid, raises `OCRNotAvailable`. Callers must check `settings.ocr_enabled and is_available(...)` before invoking.

### `app/search/queries.py`
`search(conn, query, limit=50, offset=0) -> list[SearchHit]` — FTS5 `MATCH` against `screenshots_fts`, joined back to `screenshots`, ordered by `rank`. Supports `query` like `"slack"`, `"slack OR discord"`, `"\"exact phrase\""`. `SearchHit` is a Pydantic model with `screenshot_id, captured_at, thumbnail_url, snippet, app_name, window_title`.

### `app/workers/capture_loop.py`
`async def run_capture_loop(stop_event: anyio.Event)`. Every `settings.capture_interval_seconds`: check idle, if idle for > threshold — log pause-event and skip. Otherwise capture → dedup → write thumbnail (only if NOT a near-dup of the most recent representative) → insert row → enqueue OCR job. On exception — log error event, sleep 5s, continue.

### `app/workers/ocr_worker.py`
`async def run_ocr_worker(stop_event: anyio.Event)`. Polls `screenshots WHERE ocr_status = 'pending'` LIMIT 10 every 2s. For each: load thumbnail (or original if kept), run Tesseract, UPDATE `ocr_text`, `ocr_status = 'done'`. If Tesseract not available → mark all pending as `skipped`. Resumable: just sets status to `pending` for old rows when Tesseract is enabled later.

### `app/workers/retention.py`
`async def run_retention(stop_event)`. Every hour: delete thumbnail files for screenshots older than `settings.retention_days`. Keep DB rows (they are tiny). Log cleanup stats as a capture_event.

### `app/web/main.py`
FastAPI app. Lifespan handler: init DB, start capture loop, OCR worker, retention worker as background tasks; on shutdown — signal stop. Mounts static files. Templates via Jinja2.

### `app/web/routes/timeline.py`
- `GET /` — timeline view, latest 200 events grouped by hour
- `GET /timeline?date=YYYY-MM-DD` — filter by date

### `app/web/routes/search.py`
- `GET /search?q=...` — HTMX endpoint, returns search results partial

### `app/web/routes/screenshot.py`
- `GET /screenshot/{id}` — detail view: thumbnail, OCR text, metadata, neighbours

### `app/web/routes/settings.py`
- `GET /settings` — form
- `POST /settings` — update `.env` overrides in DB-backed kv store (not actual .env, since file edits would need restart)
- `POST /api/capture/start` / `POST /api/capture/stop` / `POST /api/capture/pause`

### `app/web/routes/stats.py`
- `GET /stats` — JSON + HTML dashboard: events/day chart, top apps, dedup ratio, disk usage, OCR completion %

### `app/web/templates/`
- `base.html` — Tailwind via CDN, Alpine via CDN, HTMX via CDN, dark theme, nav bar
- `timeline.html`, `search.html`, `screenshot.html`, `settings.html`, `stats.html`
- Partials: `_screenshot_card.html`, `_search_result.html`

## Code quality targets

- `ruff format` clean
- `ruff check` with rules `E F I UP B S A RUF SIM TCH PL` clean
- `mypy --strict app` clean (modules listed in `tool.mypy.overrides` are exempt for type-stubs reasons)
- `pytest -q` green for `tests/capture/`, `tests/storage/`, `tests/dedup/`, `tests/search/`
- No `# type: ignore` without a reason comment
- No `print()` — use `structlog`

## Forbidden

- Network calls to anything except `127.0.0.1` (no telemetry, no auto-update, no analytics)
- `requests` / `httpx` in production code paths (only allowed in tests)
- `eval`, `exec`, `pickle.loads` on untrusted data
- Hardcoded paths — everything via `settings`
- Threading — async only, single event loop
- Pushing to GitHub or any remote — purely local until human review

## Run instructions for the developer

```powershell
cd C:\www-Yaroslav\Persona
uv sync --all-extras
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run uvicorn app.web.main:app --host 127.0.0.1 --port 8765 --reload
```

Open http://127.0.0.1:8765.

## Definition of done for v0

- [ ] App starts without errors via `uvicorn`
- [ ] "Start capture" button works, captures every 5s
- [ ] Timeline shows real screenshots with timestamps and app names
- [ ] Search returns relevant results (FTS5 on window titles even without OCR)
- [ ] Settings persist across restarts
- [ ] Stats dashboard shows real data
- [ ] All tests green
- [ ] All linters green
- [ ] README has working quickstart that a fresh dev can follow
