# Persona

**Your personal AI memory. 100% local. Zero cloud. Zero subscription.**

Persona is an open-source second brain that runs entirely on your own machine. It captures your screen, OCRs every frame, and builds a private, searchable timeline of your digital life. Ask it "what was that PDF I was reading on Tuesday?" — get an answer in milliseconds. No account, no signup, no telemetry.

---

## What it is

- **Open source** — AGPL-3.0-or-later. Audit every line.
- **100% local** — your data lives in a SQLite file on your disk. The HTTP server binds to `127.0.0.1` by default and is not reachable from the network.
- **Zero cloud** — there is no Persona cloud. There is no Persona company in the payment loop. There is no "free tier."
- **BYO LLM** — if you want AI-generated digests / Q&A / auto-tags, you plug in your own API key (Anthropic, OpenAI, Groq, …) and the requests go directly from your computer to the provider, billed to your card. Persona never sees a token.
- **Production-ready** — single-machine, single-user, batteries included. ~100+ HTTP endpoints, ~25 background workers, hundreds of tests.

---

## Quick start

Requires **Python 3.12+** and [uv](https://docs.astral.sh/uv/).

```powershell
git clone https://github.com/SwairIt/Persona.git
cd Persona
uv sync
copy .env.example .env
uv run uvicorn app.web.main:app --host 127.0.0.1 --port 8765 --reload
```

Open <http://127.0.0.1:8765> in your browser. On first run you will land on **`/setup`** — a guided wizard that walks you through:

1. Confirming the data folder location.
2. Picking a theme and capture cadence.
3. (Optional) Pointing at a Tesseract binary to enable OCR.
4. (Optional) Pasting a BYO LLM API key for digests and Q&A.
5. (Optional) Setting a vault master password for encrypted notes and backups.

Once the wizard finishes, click **Start capture** in Settings and the timeline begins to fill up.

### Enabling OCR

1. Install Tesseract (Windows: <https://github.com/UB-Mannheim/tesseract/wiki>).
2. Edit `.env`:
   ```
   PERSONA_TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
   PERSONA_OCR_ENABLED=true
   ```
3. Restart. New captures are OCR-indexed; old ones are backfilled in the background by the OCR worker.

---

## Headless / run anywhere (Docker)

Persona ships a `Dockerfile` and `docker-compose.yml` so you can run the web
server on any host (a VPS, a NAS, a CI box) without installing Python or `uv`.

```bash
docker compose up -d
# → http://localhost:8000
```

Data lives in the `./data` folder on the host (mounted at `/data` inside the
container), so the database, thumbnails, inbox, and backups survive container
rebuilds. Stop with `docker compose down`; your timeline stays put.

**Important caveat — capture does NOT work in a container.** A Docker
container has no display, no X server, and no Windows desktop session, so
`mss` cannot grab the screen and `pygetwindow` cannot read the active window.
There is no screenshot pipeline inside the container. For that reason the image
runs in **LEAN mode** (`PERSONA_LEAN_MODE=1`): the ~40 background workers
(capture, OCR, retention, schedulers…) are disabled and only the web server
runs.

So Docker = **headless / agent-ingest mode**, not the full capture experience:

- Browse and search a `data/` folder produced elsewhere (copy your existing
  `data/` next to the compose file before `up`).
- Receive uploads from external agents (e.g. the `mac-agent/` daemon) over
  HTTP and serve the timeline / API.
- Run the read-only web UI, exports, feeds, and `/api/*` on a server.

If you want live screen capture, run Persona natively on the machine whose
screen you want to record (see **Quick start** above) — that is the supported
capture path.

OCR of *uploaded* images still works inside the container: the base image
includes `tesseract-ocr` (with the `rus` language pack). An optional `ollama`
service is included (commented out) in `docker-compose.yml` for BYO local-LLM
digests/chat.

---

## Key features

**Timeline & search**
- Day-by-day scrollable timeline with thumbnails, app names, window titles, OCR snippets.
- **FTS5 full-text search** over OCR text + window titles + app names — sub-millisecond queries on millions of rows.
- Optional **semantic search** via local ONNX embeddings (`fastembed` + `multilingual-e5-small`) — find by meaning, not just keywords. Hybrid keyword + semantic ranking.
- Search facets, saved searches, autocomplete, regex rules.

**Capture pipeline**
- Cross-platform screen capture via `mss`, active-window detection via `pygetwindow`.
- Perceptual-hash dedup skips near-duplicate frames.
- Adaptive cadence: speeds up when you switch apps, slows down when idle.
- Battery-aware throttling, lock-aware pause, configurable quiet hours, per-app whitelist / blocklist / capture-skip rules.
- Rate guard against runaway capture (warn + auto-pause thresholds per hour).

**OCR with redaction**
- Tesseract-backed, multilingual (`eng+rus` out of the box).
- **Redaction rules** strip secrets (emails, phone numbers, regex-defined patterns) from OCR text *before* it hits the database.
- Optional **LLM vision fallback** for low-confidence frames (Anthropic Claude vision).
- Per-language stats, error-rate dashboard, find-and-replace across the corpus.

**BYO LLM digests**
- Daily TL;DR, weekly digest, monthly retrospective — all generated by your own LLM with your own key.
- Per-app digests, auto-tagging, note drafting, Q&A over your timeline.
- Anti-FOMO mode: qualitative themes only, no counts / percentages / "productivity score" guilt.
- Scheduler workers fire just before midnight so the next morning loads instantly.

**Sharing & export**
- **Share links** with optional expiry and access analytics — share a single screenshot, a day, or a curated collection.
- PDF / ZIP / NDJSON / CSV / ICS exports.
- RSS / Atom feeds (optionally token-gated) for daily and weekly digests.
- Webhooks with HMAC signing and retry queue.
- SMTP delivery for daily emails, weekly stats, day-end summaries.

**Encrypted vault**
- AES-encrypted notes for credentials, journal entries, anything sensitive.
- One master password unlocks the vault for the session.
- **Encrypted nightly DB backups** — passphrase pulled from the vault, snapshots pruned by retention policy.

**Retention & size budget**
- Tiered storage: hot → warm (lower-res thumbs at 1d in v1.13) → cold (metadata only at 30d) → archive (180d+).
- Pinned screenshots are never demoted.
- Default target: **≤25 MB per day on disk** (v1.13). Budget enforcer raises throttle level (0→3) when projected EoD usage approaches the cap.
- Live MB readout in the header; 14-day budget chart on Stats.
- Soft-delete recycle bin with configurable retention.

**Hierarchical memory (v1.14)**
- Tier 0: raw screenshots + audio segments (FTS5-indexed).
- Tier 1: **hourly cards** — heuristic markdown summary per completed hour with apps, top words, transcript excerpt. Worker writes one per hour, 30d retention. Searchable by /ask.
- Tier 5: **daily pins** — 200-byte micro-summaries that survive every retention sweep. 73 KB/year, forever. Even after raw frames are gone you can still answer "what did I do roughly on 2026-03-14?".
- View at `/memory` — last 24 hours + last 30 days side by side.

**Voice & mic kill-switch (v1.14)**
- Opus 6 kbps narrowband + WebRTC VAD by default (~4 KB of deps, no torch required).
- Click 🎙 in the header to silently pause the audio worker — perfect for "I'm watching a film in headphones, stop recording the room". Resume with the same button. Effect within ~5 seconds, no daemon restart.
- Optional Whisper transcription (opt-in, costs ~244 MB model cache).

**LLM provider — free option first (v1.14)**
- Default suggestion is **Google Gemini** — 1M tokens/day and 1500 requests/day free, no credit card.
- Other providers: Anthropic Claude (Haiku/Sonnet/Opus), OpenAI GPT-4o-mini, Groq Llama 3.3.
- All BYO. Persona never proxies your queries.

**Remote-agent uploads (v1.12)**
- Optional Mac daemon (`mac-agent/`) captures screen + speech and pushes to your Persona server over HTTPS.
- Logs rotate at 5 MB × 5 files. macOS notifications on start, 401-auth-fail, and uncaught crash.
- `persona-agent status` shows today's upload counts.

**Dashboards**
- Stats page: top apps, top windows, hour histogram, heatmap, streak, idle stats, OCR length, language mix.
- Health dashboard: capture-loop heartbeat, OCR backlog, embedding backlog, disk usage, worker liveness.
- Day kanban, day scrubber, day collage, visual diff between two days, multi-day diff.
- Reading-time estimate, focus / quiet-hours tracker, calendar view.

**PWA + browser extension**
- Installable **Progressive Web App** — pin Persona to your taskbar, works offline against your local server.
- **Browser extension** (Chromium-based, MV3) — one-click "import this tab" into Persona, popup search across your timeline.

**API surface**
- **~100+ HTTP endpoints** — HTML pages plus a `/api/*` JSON layer.
- Optional bearer-token auth on `/api/*` (off by default for the local UI).
- Personal API tokens, scoped feed tokens, audit log, audit RSS, audit replay.
- Diagnostics bundle export for support / debugging.

---

## Architecture

| Layer | Tech |
|---|---|
| Runtime | Python 3.12+, `uv` for dependency management |
| Web | FastAPI + Uvicorn |
| Templates | Jinja2 (server-rendered HTML) |
| Frontend | Tailwind (CDN) + HTMX + Alpine.js — **no Node.js build step** |
| Database | SQLite with FTS5 (built into stdlib `sqlite3`, no extension required) |
| Capture | `mss` (screen), `pygetwindow` (active window), `imagehash` (dedup), `Pillow` (WebP) |
| OCR | `pytesseract` wrapping the Tesseract binary (installed separately) |
| Embeddings | `fastembed` running ONNX locally — optional extra |
| Config | `pydantic-settings` v2 |
| Logging | `structlog` |
| Background work | asyncio workers — capture loop, OCR, embeddings, retention, digests, schedulers, inbox |

```
app/
├── capture/        # screen + window + idle + adaptive cadence
├── dedup/          # perceptual hashing
├── ocr/            # tesseract + redaction + language detection
├── embeddings/     # local ONNX semantic search
├── llm/            # BYO digests, Q&A, auto-tag, vision OCR
├── search/         # FTS5 + hybrid ranking
├── storage/        # SQLite schema, migrations, thumbnails, retention
├── vault.py        # encrypted notes / backups
├── workers/        # background loops (capture, OCR, retention, schedulers…)
├── web/
│   ├── main.py     # FastAPI app factory
│   ├── routes/     # ~200 route modules
│   ├── templates/  # Jinja2 templates
│   ├── static/     # CSS, JS, PWA manifest, service worker
│   └── middleware/
└── settings/       # pydantic-settings config
```

---

## Configuration

All configuration is via environment variables (or `.env` in the project root). Every variable is prefixed with `PERSONA_`. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `PERSONA_DATA_DIR` | `./data` | Root for the DB, thumbnails, inbox, backups |
| `PERSONA_DB_PATH` | `./data/persona.db` | SQLite database file |
| `PERSONA_THUMBNAILS_DIR` | `./data/thumbnails` | WebP thumbnail tree |
| `PERSONA_HOST` | `127.0.0.1` | Bind host — keep on loopback unless you know what you're doing |
| `PERSONA_PORT` | `8765` | HTTP port |
| `PERSONA_CAPTURE_INTERVAL_SECONDS` | `5` | Base capture cadence |
| `PERSONA_ADAPTIVE_CADENCE_ENABLED` | `true` | Speed up on activity, slow down on idle |
| `PERSONA_BATTERY_AWARE_ENABLED` | `true` | Throttle on battery / pause on critical |
| `PERSONA_IDLE_THRESHOLD_SECONDS` | `300` | Pause after N seconds with no input |
| `PERSONA_DEDUP_HAMMING_THRESHOLD` | `4` | Perceptual-hash distance for dedup |
| `PERSONA_SMART_THUMBNAIL` | `true` | Drop image when same-app frame repeats |
| `PERSONA_DAILY_SIZE_BUDGET_MB` | `4` | Header readout target |
| `PERSONA_TIER_WARM_AFTER_DAYS` | `7` | Demote thumbs to lower-res |
| `PERSONA_TIER_COLD_AFTER_DAYS` | `30` | Drop image, keep metadata + OCR |
| `PERSONA_RETENTION_DAYS` | `180` | Hard delete after N days |
| `PERSONA_RECYCLE_RETENTION_DAYS` | `7` | Recycle-bin grace period |
| `PERSONA_OCR_ENABLED` | `false` | Master OCR switch |
| `PERSONA_TESSERACT_PATH` | *(empty)* | Path to `tesseract` binary |
| `PERSONA_TESSERACT_LANGS` | `eng+rus` | Tesseract language packs |
| `PERSONA_IMAGE_BLUR_ENABLED` | `false` | Blur thumbnails on disk |
| `PERSONA_EMBEDDINGS_ENABLED` | `false` | Local semantic search |
| `PERSONA_EMBEDDINGS_MODEL` | `intfloat/multilingual-e5-small` | ONNX model |
| `PERSONA_BYO_API_PROVIDER` | *(empty)* | `anthropic`, `openai`, `groq`, … |
| `PERSONA_BYO_API_KEY` | *(empty)* | Your own key — never leaves your machine |
| `PERSONA_LLM_VISION_ENABLED` | `false` | Use BYO vision LLM as OCR fallback |
| `PERSONA_AUTO_DIGEST_ENABLED` | `false` | Daily TL;DR scheduler |
| `PERSONA_WEEKLY_DIGEST_ENABLED` | `false` | Weekly digest scheduler |
| `PERSONA_MONTHLY_DIGEST_ENABLED` | `false` | Monthly retrospective scheduler |
| `PERSONA_DAY_END_SUMMARY_ENABLED` | `false` | Pre-midnight TL;DR primer |
| `PERSONA_ANTI_FOMO_DIGEST` | `false` | Qualitative-only digests, no counts |
| `PERSONA_CLIPBOARD_HISTORY_ENABLED` | `false` | Capture clipboard text snippets |
| `PERSONA_INBOX_ENABLED` | `true` | Watch `data/inbox` for `*.md` notes |
| `PERSONA_API_AUTH_REQUIRED` | `false` | Require bearer token on `/api/*` |
| `PERSONA_FEED_AUTH_REQUIRED` | `false` | Require token on `/feeds/*` |
| `PERSONA_AUTO_BACKUP_ENABLED` | `false` | Nightly encrypted DB snapshot |
| `PERSONA_AUTO_BACKUP_PATH` | `./data/backups` | Snapshot directory |
| `PERSONA_AUTO_BACKUP_KEEP_DAYS` | `14` | Snapshot retention |
| `PERSONA_CAPTURE_RATE_WARN_PER_HOUR` | `60` | Log warning at this rate |
| `PERSONA_CAPTURE_RATE_PAUSE_PER_HOUR` | `200` | Auto-pause at this rate (`0` disables) |
| `PERSONA_THEME` | `dark` | UI theme |
| `PERSONA_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

See `.env.example` for the full list and `app/settings/config.py` for the authoritative schema.

---

## Data folder layout

```
data/
├── persona.db              # the entire database — your timeline lives here
├── persona.db-wal          # SQLite write-ahead log
├── persona.db-shm          # SQLite shared-memory file
├── thumbnails/             # WebP thumbnails, sharded by yyyy/mm/dd
│   └── 2026/06/03/<id>.webp
├── inbox/                  # drop *.md files here, the inbox worker imports them
│   ├── processed/          #   → success goes here
│   └── failed/             #   → failures here with a sibling .error.txt
└── backups/                # nightly encrypted snapshots (when enabled)
    └── persona-YYYYMMDD-HHMMSS.db.enc
```

Move the entire `data/` folder to another disk and point `PERSONA_DATA_DIR` at it — that is the whole migration story.

---

## Privacy and trust model

**What stays on your machine, always:**
- Every screenshot.
- Every OCR string.
- Every embedding vector.
- Every note, tag, digest, share-link target.
- The encrypted vault and every backup.

**What can leave your machine, only when you explicitly opt in:**
- LLM digest / Q&A / auto-tag prompts — sent to **the provider whose key you pasted**, using **your account**, billed to **your card**. Persona has no API, no proxy, no telemetry endpoint.
- Webhook payloads — only to URLs you configure.
- Share-link page views — only when you create a share link and give it to someone.
- SMTP emails — only when you configure outbound SMTP credentials.

**Defaults that protect you:**
- Server binds to `127.0.0.1`. Not on your LAN.
- Capture auto-pauses on screen lock and after `idle_threshold_seconds` of no input.
- Quiet hours skip capture entirely.
- Redaction rules scrub secrets from OCR before they hit the database.
- Anti-FOMO digest mode is available if metrics make you anxious.
- One-click **pause capture**, one-click **wipe today**, one-click **diagnostics bundle** for support without leaking your data.

**What we cannot protect you from:**
- A user who pastes their own LLM API key and then complains the LLM provider saw their data. It does — that is what BYO means.
- A user who opens `PERSONA_HOST` to `0.0.0.0` on an untrusted network without setting `PERSONA_API_AUTH_REQUIRED=true`.
- Anyone with physical or remote access to your unlocked machine — Persona is a memory, not a fortress. Use the vault for genuinely sensitive material and enable encrypted backups.

If you find a security issue, please open a private security advisory on the GitHub repository.

---

## Roadmap

Persona is at **v1.0** — feature-complete for the single-machine, single-user use case. The roadmap from here is intentionally small:

- **macOS and Linux capture parity.** Windows is the daily-driver platform today; the capture path is mostly cross-platform via `mss` but needs hardening, packaging, and CI on the other two.
- **One-click installers** for non-developer users (signed binaries, no `uv` required to start).
- **Multi-device sync** via the user's own object storage (S3 / B2 / WebDAV) — encrypted end-to-end, no Persona server in the path.
- **More BYO LLM providers** out of the box (currently Anthropic / OpenAI / Groq; community PRs welcome for Ollama, llama.cpp, OpenRouter, etc.).

Explicitly **not** planned:
- Persona-hosted cloud. There will not be one.
- A paid tier. There will not be one.
- Mobile native apps with background screen capture. Apple and Google both block this; the PWA + browser-extension path is as far as we go.
- Built-in gamification, social features, "productivity scores," or engagement metrics. This is a memory, not a slot machine.

---

## License

AGPL-3.0-or-later. See `LICENSE`.
