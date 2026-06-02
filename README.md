# Persona

Open-source personal AI memory. Captures your screen, builds a searchable log of your digital life. 100% local, zero cloud, zero subscription.

## What it does

Persona takes a screenshot every few seconds, runs OCR to extract on-screen text, and stores everything in a local SQLite database. You can then search your past — "what was that PDF I was reading on Tuesday" — and get an answer in milliseconds.

No data ever leaves your machine. The database file is on your disk. No cloud account, no signup, no telemetry. If you want AI on top (LLM summarisation, semantic search), you plug in your own API key from Anthropic / OpenAI / Groq — the requests go directly from your computer to the provider, billed to your card. We are not in the payment loop.

## Quick start (Windows)

```powershell
cd C:\www-Yaroslav\Persona
uv sync
copy .env.example .env
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Open http://127.0.0.1:8765 in your browser. Click **Start capture** in settings.

### Enabling OCR (optional)

1. Install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki
2. Find the install path (default: `C:\Program Files\Tesseract-OCR\tesseract.exe`)
3. Edit `.env`:
   ```
   PERSONA_TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
   PERSONA_OCR_ENABLED=true
   ```
4. Restart Persona. New captures are OCR-indexed; old ones are backfilled in the background.

## Architecture

| Module | Responsibility |
|---|---|
| `app/capture/` | Screen capture via `mss`, active window detection via `pygetwindow` |
| `app/storage/` | SQLite schema with FTS5, WebP thumbnail writer, retention cleanup |
| `app/dedup/` | Perceptual hash (`imagehash`) — skip near-duplicate frames |
| `app/ocr/` | `pytesseract` wrapper (latent — activates when Tesseract binary is installed) |
| `app/search/` | Full-text search over OCR + window titles + app names |
| `app/web/` | FastAPI + Jinja2 + HTMX/Alpine UI: timeline, search, settings, stats |
| `app/workers/` | Background capture loop (asyncio), OCR worker, retention worker |
| `app/settings/` | Pydantic-settings config from `.env` |

## Stack

- Python 3.12+
- FastAPI + Uvicorn
- SQLite with FTS5 (built into stdlib `sqlite3` — no extension required)
- `mss` for cross-platform screen capture
- `Pillow` for WebP encoding
- `imagehash` for perceptual dedup
- `pytesseract` for OCR (binary installed separately by user)
- `pydantic-settings` for typed config
- HTMX + Alpine.js + Tailwind (CDN) for UI — no Node.js build step

## Disk budget

Default v0.3 settings target **2-4 MB per active day** thanks to smart-thumbnail + tiered storage. Older screenshots demote to lower resolution after 7d and lose their image entirely after 30d (metadata + OCR + embedding stay searchable forever).

## Privacy model

- Screenshots and OCR live in `data/` on your disk only.
- The app binds to `127.0.0.1` by default — not reachable from network.
- No telemetry, no analytics, no auto-update calls.
- Capture pauses automatically when the screen is locked or no input for 5+ minutes.
- You can pause/resume from the UI or kill the process at any time.

## v0.3 — what's new (size-budget release)

- 🎯 **Smart-thumbnail capture** + **tiered storage** — typical 8h workday now lands in **2-4 MB on disk**. Same-app frames within `smart_min_gap_seconds` keep only metadata + OCR. Older screenshots demote to lower resolution; ancient ones drop the image entirely but keep the searchable text.
- 📌 **Pin** important screenshots so they're never demoted.
- 📊 Live MB readout in the header + new "Today's size budget" section on Stats with 14-day chart.
- 🔧 `scripts/recompress_tiers.py` for manual rebalance after settings changes.

## v0.2 — what's new

- **Semantic search** via local ONNX embeddings (`fastembed` + `intfloat/multilingual-e5-small`). Search by meaning, not just keywords. Try it: ask "what was that auth bug?" — even if those exact words never appeared on screen. Enable with `uv sync --extra embeddings` + `PERSONA_EMBEDDINGS_ENABLED=true`.
- **Hybrid ranking** — keyword + semantic results merged into one list, mode toggle in the search UI.
- **Journal view** (`/journal`) — every screenshot you wrote a note on, grouped by day. Markdown-rendered.
- **Markdown notes** with Write/Preview toggle.
- **Help page** (`/help`) — keyboard shortcuts, search-mode guide, privacy reminders.
- **Bulk delete** by app or by date range in Settings → Danger zone.
- **Top windows** alongside Top apps in Stats.
- **Indexing-status badge** in the header — see how many screenshots still need OCR or embedding.
- **Orphan-thumbnail cleanup** script.

## Status

v0.2 — single-machine, single-user, local-only. macOS and Linux support planned. Mobile not planned (Apple blocks background screen capture).

## License

AGPL-3.0-or-later
