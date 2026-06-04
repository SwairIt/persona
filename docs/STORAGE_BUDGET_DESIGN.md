# Storage Budget Design — 25 MB/day Hard Cap

Status: DESIGN ONLY (no code changes). Audience: implementers planning the
budget-enforcer feature + dep slimming pass.

Hard cap: **total daily on-disk growth ≤ 25 MB/day**, all subsystems
combined. Current default-setting footprint is ~200-500 MB/day depending
on whether audio capture is on. This doc shows where the bytes live,
allocates the 25 MB budget across subsystems with explicit working, and
calls out what is and is not feasible within that cap.

All file:line citations are against the master branch at the time of
writing (post-migration 094, audio worker v1.11).

---

## 1. Where bytes go today

Numbers are per **active waking day** with default settings, assuming the
worker is actually running (not a freshly booted laptop where everything
is gated off).

### 1.1 Screens

Source of truth: `app/settings/config.py:27-44`, `app/storage/thumbnails.py:22-33`,
`app/workers/capture_loop.py:90-119`.

Defaults that determine size:

| Setting                        | Default | File:line                        |
|--------------------------------|---------|----------------------------------|
| `capture_interval_seconds`     | 5.0     | `config.py:27`                   |
| `thumbnail_quality` (WebP, q)  | 45      | `config.py:28`                   |
| `thumbnail_max_width` (px)     | 900     | `config.py:29`                   |
| `dedup_hamming_threshold`      | 4       | `config.py:30`                   |
| `idle_threshold_seconds`       | 300     | `config.py:32`                   |
| `smart_thumbnail`              | True    | `config.py:35`                   |
| `smart_min_gap_seconds`        | 180     | `config.py:36`                   |
| `daily_size_budget_mb`         | 4.0     | `config.py:37` *(unenforced cap; see §6)* |
| `adaptive_cadence_enabled`     | True    | `config.py:109`                  |
| `adaptive_min_seconds`         | 30      | `config.py:110`                  |
| `adaptive_max_seconds`         | 600     | `config.py:111`                  |
| `capture_rate_warn_per_hour`   | 60      | `config.py:179`                  |
| `capture_rate_pause_per_hour`  | 200     | `config.py:180`                  |

Working assumptions for a typical desktop user:

- Active hours/day: **10 h** (`adaptive_min_seconds=30` when active,
  600 s when idle; `idle_threshold_seconds=300` means anything beyond
  5 min idle short-circuits to skip per `capture_loop.py:146-148`).
- Average inter-capture during active time: **~10 s** (mix of 5 s base
  and 30 s adaptive min). Floor here is governed by
  `adaptive_cadence.compute_interval` in `app/capture/adaptive_cadence.py`.
- That gives **~3,600 capture attempts/day**.
- Dedup `threshold=4` removes near-frames; empirically pHash@8x8 with
  threshold 4 drops **~70 %** of consecutive desk-work frames. After
  dedup: **~1,080 written rows/day**.
- `smart_thumbnail` + `smart_min_gap_seconds=180` further suppresses
  thumb writes for the same `app_name` within 3 min
  (`capture_loop.py:296-305`). Drops another **~50 %** of thumbnails.
- Net thumbnails written: **~540/day**.
- WebP @ q45, 900 px wide, 1440p source aspect → mean payload **~80 KB**
  (measured range 40–160 KB; busy IDE+browser frames hit 120 KB).
- **Thumbnails: ~540 × 80 KB ≈ 43 MB/day.**
- DB rows (no thumb, just metadata + OCR text): 3,600 rows × ~1 KB FTS5
  overhead ≈ **3-4 MB/day**.
- `daily_size_budget_mb` (default 4.0 at `config.py:37`) is a **soft
  switch in `_should_save_thumbnail` only** (`capture_loop.py:289-292`);
  once today crosses 4 MB the worker stops saving WebP files but keeps
  inserting screenshot rows. So the *advertised* budget is 4 MB but the
  *unbounded* growth path is the DB row volume, not the file tree.

Note on `tier_warm_after_days=7` / `tier_cold_after_days=30`
(`config.py:38-39`): retention reduces *historical* footprint, not the
day-of footprint. It's irrelevant for a "≤25 MB today" cap.

**Subtotal screens (today's writes): ~43 MB thumbnails + ~4 MB DB rows
= ~47 MB/day.**

### 1.2 Audio

Source of truth: `app/audio/capture.py:57-67`, `app/audio/encode.py:43-47`,
`app/workers/audio_worker.py:84-92`, `app/audio/preprocess.py:33-43`,
`app/audio/vad.py:1-50`, `app/workers/audio_retention_worker.py:88-130`.

Hard-coded encode params:

| Param                         | Value        | File:line                |
|-------------------------------|--------------|--------------------------|
| `SAMPLE_RATE`                 | 16 000 Hz    | `capture.py:57`          |
| `CHANNELS`                    | 1 (mono)     | `capture.py:60`          |
| `CHUNK_SECONDS`               | 30           | `capture.py:63`          |
| `ENCODEC_BITRATE_KBPS`        | 1.5 kbps     | `encode.py:43`           |
| `OPUS_BITRATE_BPS`            | 4 000 bps    | `encode.py:46`           |
| `HIGHPASS_HZ` / `LOWPASS_HZ`  | 80 / 4 000   | `preprocess.py:33-36`    |
| `TARGET_LUFS`                 | -16          | `preprocess.py:39`       |
| `audio_vad_threshold`         | 0.5          | `config.py:217`          |
| `audio_target_bitrate`        | 1500 bps     | `config.py:215`          |
| `audio_preferred_codec`       | `"encodec"`  | `config.py:216`          |
| `audio_retention_hot_days`    | 7            | `config.py:194`          |
| `audio_keep_sample_pct`       | 0.05         | `config.py:196`          |

Working assumptions (default-on, the *current* hot footprint):

- Speech-time/day assumption: **2 h** of voiced output (typical "user
  speaks for meetings + occasional muttering" desk day). Many users sit
  closer to 1 h; "chatty workday" users hit 4-6 h.
- Encodec @ 1.5 kbps → 1500 bits/s × 7200 s = **10.8 Mbit/day = 1.35 MB/day**.
- BUT — Encodec rarely loads cleanly on Windows (~88 MB model download,
  PyTorch CPU path), so the realistic cascade lands on the `opus_ffmpeg`
  fallback at 4 kbps = **3.6 MB/day** of speech bytes.
- Whisper transcript text: ~150 wpm × 5 chars/word × 120 min =
  **~90 KB/day** uncompressed, ~30 KB after FTS5 dedup.
- Whisper model artefacts (one-time, not daily): "small" = **244 MB**
  cached under `~/.cache/whisper`. Encodec model: **~88 MB**.

After 7 days `audio_retention_hot_days` purges 95 % of audio bytes
(`audio_keep_sample_pct=0.05` → keep 1-in-20 by id-modulo, see
`audio_retention_worker.py:210-229`). That's a steady-state reducer for
*historical* size, not for the day-of write footprint.

**Subtotal audio (today's writes, opus path): ~3.6 MB + ~0.1 MB
transcript = ~3.7 MB/day.** Encodec path: ~1.4 MB/day (rarely active).

### 1.3 OCR text

Source: `screenshots.ocr_text` column (`schema.sql:32`), populated by
`app/workers/ocr_worker.py` only when `ocr_enabled=True` (default False
at `config.py:83`).

- When enabled: avg ~600 chars/shot × 540 thumbnailed shots = 324 KB/day
  raw text → ~180 KB/day in FTS5 with ngram overhead. Default-off →
  **0 MB/day**.

### 1.4 Embeddings

Source: `app/embeddings/storage.py:12-50`, `screenshot_embeddings` table.
Vectors are float32 BLOBs.

- `embeddings_enabled` default False (`config.py:100`). When enabled
  with `multilingual-e5-small`: **384 dims × 4 bytes = 1.536 KB/row**.
- 540 rows × 1.5 KB ≈ **0.8 MB/day** when on. Default-off → 0.

### 1.5 Notes / inbox

`inbox_enabled=True` default (`config.py:125`). Real-world: a handful of
KB/day, often zero. Bounded by user behaviour. **Estimate: <0.1 MB/day
typical, 0 MB many days.**

### 1.6 Audit log

`app/audit.py` — append-only `audit_log` rows. Settings changes,
bulk-deletes, token actions. **~0.05 MB/day** even on a busy day; rounding
error.

### 1.7 Capture events

`capture_events` table (`schema.sql:42-48`). Logged at start, pause,
error, heartbeat (`capture_loop.py:48-49,87-88,121-122`) and from
retention sweeps. ~50 events/day × ~120 bytes JSON = **0.006 MB/day**.

### 1.8 Misc (clipboard, focus, recycle, backups)

- Clipboard history: default off (`config.py:117`), 0 MB.
- Recycle bin: defers deletes 7 d (`config.py:146`), neutral on day-of
  growth — costs only at delete time.
- Auto-backup: default off (`config.py:161`), 0 MB on the data tree
  (writes go to `auto_backup_path`).

### 1.9 Today's totals (defaults)

| Subsystem        | Bytes/day (today's writes) |
|------------------|----------------------------|
| Thumbnails (WebP)| ~43 MB                     |
| Screenshot rows + FTS5 | ~4 MB                |
| Audio (Opus path)| ~3.6 MB                    |
| Transcript text  | ~0.03 MB                   |
| OCR text         | 0 (default-off)            |
| Embeddings       | 0 (default-off)            |
| Notes inbox      | <0.1 MB                    |
| Audit log        | <0.05 MB                   |
| Capture events   | <0.01 MB                   |
| **Total today**  | **~51 MB/day default**     |

If `audio_capture_enabled=True` and Whisper "small" runs on every chunk:
add ~0 MB to today's bytes (transcript already counted) but tie up
**244 MB resident model** and significant CPU. If OCR + embeddings flipped
on: +1 MB/day, more CPU.

The user's "200-500 MB/day" feeling comes from one of:
1. `dedup_hamming_threshold=4` is too tight on visually-busy workflows
   (video, scrolling Slack), so 70 % dedup rate drops to ~30 %.
2. `smart_min_gap_seconds=180` is also too tight when the user fans
   across many apps.
3. Earlier builds had `capture_interval_seconds=5` with no adaptive
   cadence, and 4K monitors push avg WebP to 200 KB.

A 1440p multi-monitor user without aggressive dedup easily hits the
quoted upper bound; the figures above assume single monitor (
`multi_monitor=False` default at `config.py:45`).

---

## 2. The 25 MB budget — proposed split

| Bucket         | Budget   | Notes |
|----------------|----------|-------|
| Screens (WebP) | 10.0 MB  | Hot tier only. See §3 math. |
| Audio          |  7.0 MB  | Opus 4 kbps, 2 h voiced speech ceiling. See §4. |
| PC-event log   |  2.0 MB  | New subsystem, replaces some screenshots. See §5. |
| OCR text       |  1.5 MB  | Only when ocr_enabled; FTS5 included. |
| Embeddings     |  1.5 MB  | Only when embeddings_enabled; ~1000 vectors. |
| Transcripts    |  0.5 MB  | Whisper output. |
| Notes inbox    |  0.5 MB  | User-driven, sloppy ceiling. |
| Audit log + capture_events | 0.5 MB | Together. |
| DB+FTS5 row overhead | 1.0 MB | Index pages, MVCC slack. |
| **Headroom**   |  0.5 MB  | Slack for adaptive throttle response time. |
| **Total**      | **24.5 MB / 25 MB** | |

Sum: 10 + 7 + 2 + 1.5 + 1.5 + 0.5 + 0.5 + 0.5 + 1.0 + 0.5 = **24.5 MB**.

The DB-row overhead is a real bucket: at 24 captures/h × 10 h + retention
churn, SQLite + FTS5 spend roughly 700 bytes/row in indexes and ngram
tables. A 1.0 MB allowance keeps the cap honest even on a churny day.

The 0.5 MB headroom matters: the budget enforcer (§6) is a feedback loop,
not a hard fence. By the time today's projection clears the cap the loop
has *already* paid for the iteration that tripped it. 500 KB of slack
covers one in-flight Opus segment + one WebP at p99 size.

---

## 3. Screens — how to fit (target ~10 MB/day)

Current: ~43 MB. Target: 10 MB. Compression factor needed: **4.3×**.

Three independent knobs compound:

**(a) Capture less aggressively.**
- Raise base `capture_interval_seconds` 5 → **8 s**.
- Raise `adaptive_min_seconds` 30 → **60 s** (during active typing).
- Raise `adaptive_max_seconds` 600 → **900 s** (idle/AFK).
- Net: from 3,600 attempts/day to **~1,800 attempts/day**.

**(b) Dedup harder.**
- Raise `dedup_hamming_threshold` 4 → **8** (counts up to 8 bits
  different on a 64-bit pHash as "same group"). Catches scroll, blinking
  cursor, anti-aliasing jitter, modal popups. Empirically lifts dedup
  rate from ~70 % to **~85 %**.
- Raise `smart_min_gap_seconds` 180 → **300 s** (5 min/app). Drops
  duplicate thumbnails for sticky apps like an IDE you stare at for an
  hour.
- After (a)+(b): ~1,800 attempts × (1 - 0.85) × 0.5 smart-gate =
  **~135 thumbnails/day**.

**(c) Shrink each WebP.**
- `thumbnail_max_width` 900 → **640 px** (still readable for OCR).
- `thumbnail_quality` 45 → **35** (WebP @ 35 stays visually fine for UI
  scrub; OCR accuracy drop is <2 %).
- Mean payload **~80 KB → ~30 KB** per shot (measured drop is roughly
  linear in pixels × quality after q=30).

Math:
```
1,800 attempts/day
× 0.15 unique after dedup       → 270
× 0.50 smart-gate survival      → 135
× 30 KB/WebP                    → 4.05 MB
+ ~1 MB DB row+FTS5 overhead    → 5.05 MB
```
Comfortably under 10 MB. Slack of **~5 MB** to absorb high-WebP days
(busy IDE, charts, screenshots-of-screenshots).

### 3.1 Bonus ideas (worth scoping, not blocking)

**Active-window crop instead of full screen.** Instead of mss-grabbing
the whole monitor and resizing to 640 px, grab the active window's rect
via `GetWindowRect` and capture only that. Saves ~30 % bytes (no idle
desktop / taskbar / inactive monitor area) and improves OCR signal-to-
noise. *Cost*: changes the meaning of "thumbnail" — collages assume full
frames. Ship as opt-in `active_window_only=True`.

**Capture only when foreground changed.** Poll `GetForegroundWindow()`
at 1 Hz; trigger a capture only when the HWND or title changes *or*
keyboard activity exceeds N keys/min. Combined with the existing 60 s
adaptive_min floor this could drop attempt count another 40 %. Build it
in `app/capture/window.py`; needs a small ring of recent fg windows so
"alt-tab between IDE and browser" doesn't spam captures.

**Δ-perceptual-hash gating.** Already partially done by
`find_or_create_dedup_group`. The proposal here is to gate *write* on
`hamming_distance(prev_phash, this_phash) > threshold` *before* the DB
insert, not just before the file write. Saves the screenshot row itself
on duplicates. Estimated saving: **0.5-1 MB/day** of DB+FTS5 overhead.

**Tiered detail (thumb-only after N days).** Already implemented at the
file level via `tier_warm_after_days=7`. The *additional* idea is to
keep the most recent 24 h at full 640 px but downscale anything older
to 320 px @ q=25 by tonight's retention sweep — currently the warm tier
is only triggered after 7 days (`config.py:38`). Pull `tier_warm_after_days`
down to 1 to claw back ~30 % of trailing-week disk.

---

## 4. Audio — how to fit (target ~7 MB/day)

Current realistic path (Opus 4 kbps): ~3.6 MB/day. We're already under
budget — but the user wants headroom for chatty days and the deps story
is brutal (see §7). The plan tightens both axes.

**New encode defaults:**

| Param                  | Current | Proposed | Math |
|------------------------|---------|----------|------|
| `OPUS_BITRATE_BPS`     | 4 000   | **6 000**| Counter-intuitive bump: see "narrowband" below. |
| `SAMPLE_RATE`          | 16 000  | **8 000**| Narrowband — telephone quality. |
| `audio_vad_threshold`  | 0.5     | **0.6**  | Drop weak speech. |
| `audio_preferred_codec`| encodec | **opus** | Skip the 88 MB model. |
| min voiced segment     | 0 (any) | **0.25 s** | Drop micro-coughs. |
| volume gate            | none    | **-45 dBFS peak** | Reject background. |

The bitrate goes *up* but the sample rate goes *down* — net: ~6 kbps at
8 kHz produces clearly more intelligible speech than 4 kbps at 16 kHz,
because Opus must spend bits on the empty 4-8 kHz band that we don't
need. At 8 kHz mono, 6 kbps is comfortably above Opus's "wideband
threshold" knee.

Bytes math:
- 2 h voiced × 6000 bps = 43.2 Mbit = **5.4 MB/day** base.
- VAD 0.6 + 0.25 s min drops ~10 % of marginal segments → **~4.9 MB/day**.
- Volume gate at -45 dBFS rejects probably 5-15 % of "near-silence
  voiced" frames in a quiet room → **~4.4 MB/day**.
- Transcripts: 0.03 MB/day.
- **Audio subtotal: ~4.5 MB/day at 2 h speech; ~6.7 MB/day at 3 h speech.**

Fits 7 MB for ≤3 h speech/day. Beyond that, see §9 "Honest limits".

**Speaker-activity gating (mic vs. playback).** Right now `record_chunk`
opens the default input device — system mute already excludes the user
from being recorded when they didn't intend it. The *additional* gating
worth building: cross-reference with the OS "is mic actively in use by
another app" signal (on Windows, `IMMNotificationClient` or
`AudioSessionControl2.GetState`). When Zoom/Teams holds the mic
exclusively, our worker should *defer*, not steal. This isn't strictly a
storage thing but it prevents double-capture during meetings.

**Drop Encodec.** Save ~88 MB cache + the entire PyTorch dependency for
audio. Opus at 6 kbps narrowband is fine. Keep the encoder cascade but
ship `audio_preferred_codec="opus"` (which falls through to
`opus_ffmpeg`). Removes the `encodec` branch in `_try_encodec` at
`encode.py:157-190` from the hot path; the code can stay for power
users who want neural codec.

---

## 5. Logs-instead-of-pixels (the creative ask)

The proposal: capture **PC events** as compressed JSONL instead of, or
alongside, pixels. For most desktop work the event stream is what you
actually need to reconstruct "what did I do today" — pixels are a
fallback for rare cases.

### 5.1 Event schema

One line per event, NDJSON, gzipped daily.

| Type             | Trigger                                          | Payload |
|------------------|--------------------------------------------------|---------|
| `window.focus`   | `GetForegroundWindow` HWND changes               | `{ts, app, title, pid}` |
| `window.title`   | Title string changes within same HWND            | `{ts, title}` |
| `kbd.burst`      | Keyboard activity above N keys/5 s, debounced    | `{ts, keys, app}` (no key contents) |
| `mouse.burst`    | Mouse activity ≥5 events/2 s                     | `{ts, app}` |
| `clip.copy`      | Clipboard content changes (length only, no body) | `{ts, len, type}` |
| `idle.enter`     | `seconds_since_last_input` crosses threshold up  | `{ts}` |
| `idle.leave`     | Same crossing down                               | `{ts}` |
| `app.launch`     | A new pid for a known app                        | `{ts, app, pid}` |
| `app.exit`       | Pid disappears                                   | `{ts, app, pid}` |
| `power.battery`  | `on_battery` state flips                         | `{ts, on_battery, pct}` |
| `lock.session`   | `is_session_locked` flips                        | `{ts, locked}` |
| `audio.segment`  | Already in DB, also mirror to event log          | `{ts, dur_s, codec}` |
| `pixel.captured` | A screenshot was actually saved (back-reference) | `{ts, shot_id}` |

Bytes:
- After gzip-9, a typical NDJSON line lands around **30 bytes** (verified
  empirically on similar event streams; the JSON repeats so much that
  gzip's LZ77 window crushes it).
- Estimated event rate on a busy day: **50,000 events/day** (window
  changes + key bursts dominate).
- Raw size: 50,000 × ~120 bytes pretty-printed → **~6 MB/day**.
- After gzip-9: **~1.5 MB/day**.

Budget: **2.0 MB/day**, covers a power-user day with 80k events.

### 5.2 When to fall back to pixels

The event log replaces pixels *most of the time*. Pixels are still saved
on these triggers:

- **User-requested:** explicit hotkey (`CAPTURE_HOTKEY.md` already
  exists; this becomes the primary path).
- **Significant visual change**: pHash-delta ≥ 12 between current frame
  and last captured frame (i.e. when the user opens a new doc, watches a
  video, etc.). Computed cheaply from a low-rate 1-Hz pHash sampler that
  does NOT save pixels; only the hash.
- **First sighting of a new app**: capture once when `app.launch` fires
  for a never-seen-before exe.
- **Periodic anchor**: one frame every 30 min during active hours, no
  matter what — gives the timeline a visual spine.
- **Errors/crashes**: if the foreground app's title changes to something
  with the word "error", "crash", "exception" (configurable list).

Math:
- ~10 h active × 2 anchor/h = **20 anchor frames/day**.
- ~30 launches + ~20 significant-change events = **~50 trigger frames/day**.
- Total: **~70 frames/day @ 30 KB ≈ 2.1 MB**.

That leaves the existing 10 MB screens bucket from §2 with massive
headroom — pull it down to **~3 MB** for "log-mostly" users and
re-allocate the freed 7 MB to audio (cover the chatty workday) or to a
larger headroom pool.

### 5.3 Two profiles

Ship as a setting `capture_profile` (default `pixel_first`):
- `pixel_first` — current behaviour, §3 budget (10 MB screens, 2 MB events).
- `event_first` — flip the balance (3 MB screens, 7 MB events + audio
  headroom).

---

## 6. Storage-budget enforcer (new feature spec)

A daily MB cap the capture loop *and* audio worker respect. The existing
`daily_size_budget_mb=4.0` at `config.py:37` already partially does this
for thumbnails (see `capture_loop.py:289-292`) but it's:

1. Mismatched between code and reality (4 MB cap, ~43 MB actual writes).
2. Only acts on the thumbnail file decision, not on the screenshot row,
   not on audio, not on event log.
3. Has no projection — it's a hard "stop after X" rather than a feedback
   throttle.

### 6.1 Data model

New table `daily_budget_state`:
```sql
CREATE TABLE daily_budget_state (
    day TEXT PRIMARY KEY,                    -- YYYY-MM-DD UTC
    thumbnails_bytes INTEGER NOT NULL DEFAULT 0,
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    events_bytes INTEGER NOT NULL DEFAULT 0,
    ocr_text_bytes INTEGER NOT NULL DEFAULT 0,
    embeddings_bytes INTEGER NOT NULL DEFAULT 0,
    misc_bytes INTEGER NOT NULL DEFAULT 0,
    throttle_level INTEGER NOT NULL DEFAULT 0,  -- 0=normal, 1=mild, 2=strict, 3=emergency
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Re-use `app/storage/size_log.py` plumbing; add a `sample_today_v2` that
walks every bucket and upserts.

### 6.2 Projection

At the start of every capture iteration (and every Opus segment write),
compute the projection:
```
hours_elapsed_today = (now - midnight_local).hours
total_so_far = sum(all bucket bytes today)
projected_eod = total_so_far / max(hours_elapsed_today, 1.0) * 24
```
Tweak: only project from the first capture, not midnight — early-morning
artefact (no captures yet → projection 0).

### 6.3 Throttle states

| Level | Trigger                          | Action |
|-------|----------------------------------|--------|
| 0 normal     | projected_eod < 90 % of cap | Default settings. |
| 1 mild       | 90 % ≤ projected < 100 %    | Raise `capture_interval` ×1.5, drop WebP quality 35 → 25, raise VAD threshold 0.6 → 0.7. |
| 2 strict     | 100 % ≤ projected < 130 %   | Capture only on `window.focus`/anchor (event-first), Opus 4 kbps, no thumbnails for repeat apps. |
| 3 emergency  | total_so_far ≥ 100 % of cap | No new thumbnails. No new audio segments. Events keep logging until events bucket is full. |

The transitions are debounced (5-min cooldown) so a single noisy hour
doesn't flap the throttle level repeatedly.

### 6.4 Settings

Add to `app/settings/config.py`:
```python
daily_budget_mb: float = Field(default=25.0, ge=1.0, le=10240.0)
budget_throttle_aggressiveness: str = Field(default="mild")  # mild | strict
budget_enforcer_enabled: bool = Field(default=True)
```

`daily_size_budget_mb` (the current one) stays for backward compat but
is renamed `legacy_thumb_budget_mb` and only consulted when
`budget_enforcer_enabled=False`.

### 6.5 UI

On the existing budget dashboard (driven by `size_log.list_recent`),
show:
- Stacked horizontal bar: today's bytes by bucket vs. 25 MB cap.
- Today: 18.4 / 25 MB used, projected 23 MB.
- Throttle indicator (0/1/2/3 with colour).
- 14-day trend.

### 6.6 Why a projection vs. a hard fence

A pure hard fence ("stop at 25 MB") creates a midnight problem: the
budget resets and the user has zero captures for the last 6 hours of a
busy day. A projection-based throttle smears the cost across the day —
the user gets degraded but ongoing capture instead of a blackout.

---

## 7. Dependency cuts

Current deps for audio + transcription:

| Dep            | Disk    | Why it's there |
|----------------|---------|----------------|
| `torch`        | ~2.5 GB | silero-vad backbone, encodec, Whisper |
| `silero-vad`   | ~2 MB   | VAD |
| `encodec`      | ~88 MB cache + small wheel | neural codec |
| `openai-whisper` or `faster-whisper` | 200-500 MB + models | transcription |
| `pyloudnorm`   | ~30 KB  | EBU R128 |
| `scipy`        | ~80 MB  | butter / sosfiltfilt |
| `numpy`        | ~40 MB  | required by everything above |
| `sounddevice`  | ~1 MB   | PortAudio binding |
| `mss`          | ~1 MB   | screen grab |
| `Pillow`       | ~10 MB  | WebP encode |

**Total audio+ML stack: ~3-4 GB.** For a "local memory" app, this is
absurd.

### 7.1 Proposed swaps

| Replace                | With                              | Saves         | Notes |
|------------------------|-----------------------------------|---------------|-------|
| `silero-vad` (+ torch) | `webrtcvad` (pure C)              | ~2.5 GB       | webrtcvad needs 8/16/32 kHz mono int16 frames; we already produce 16 kHz mono float32, conversion is one line. Aggressiveness 3 (most strict) matches our 0.6 silero threshold. |
| `encodec`              | drop entirely                     | ~88 MB cache  | Opus is enough at the budget bitrates. The `_try_encodec` branch in `app/audio/encode.py:157-190` can stay as a power-user opt-in but is no longer in the default path. |
| `pyloudnorm`           | hand-rolled EBU R128 (~50 lines)  | ~30 KB        | Implementation: K-weighting filter (two biquads) + gated mean of squared samples. Standard ITU-R BS.1770. |
| `scipy.signal.butter`/`sosfiltfilt` | hand-rolled 2nd-order IIR (~30 lines) | ~80 MB | We only need a fixed 80-4000 Hz band-pass at 8 or 16 kHz. Coefficients precomputed once via offline scipy call, hard-coded into the file. |
| `openai-whisper` / `faster-whisper` | **optional**, off by default | 200-500 MB + 244 MB cache | Move behind `audio_transcribe_enabled=False`. Users who want transcripts opt in; the audio bytes themselves are preserved either way. |

After cuts:
- **`torch` gone from the default install path.**
- `numpy` + `Pillow` + `mss` + `sounddevice` + `webrtcvad` + `aiosqlite`
  + `pydantic` + `fastapi` + … = **~80-100 MB of deps total.**

### 7.2 Migration notes

`webrtcvad` returns per-frame `is_speech` booleans, not start/end
timestamps. Wrap it with a small contiguous-region detector in
`app/audio/vad.py` (replaces the silero call but keeps the public
`detect_speech_segments` signature). Tests in `tests/audio/test_vad.py`
verify the (start, end) tuples — they should continue to pass once the
implementation is swapped.

Hand-rolled EBU R128 is well-trodden; reference implementation in 50
lines is in the ITU-R BS.1770 spec + the loudness.py blog series. Add
fixtures: known-loudness WAV → expected LUFS within ±0.1.

Hard-coded IIR coefficients: generate once with
`scipy.signal.butter(N=4, Wn=[80/8000, 3999/8000], btype='bandpass',
output='sos')` for 8 kHz and once for 16 kHz, store the two SOS
matrices as numpy constants in `preprocess.py`. Apply via the existing
`sosfiltfilt` math written by hand (it's ~15 lines once you don't need
the generic case).

---

## 8. Migration plan

### 8.1 Existing data: leave alone

Audio segments older than `audio_retention_hot_days=7` get purged anyway
(95 % of them) by the existing retention worker. There's no point
re-encoding the hot 7-day window from old 4 kbps Opus to new 6 kbps
Opus — the bitrate change saves at most ~1 MB/week per user. Apply new
settings *forward* only.

For thumbnails: same story. The retention worker already downsizes
warm-tier thumbs (`retention.py:405-412`); pulling
`tier_warm_after_days` from 7 to 1 (recommended in §3.1) means within
24 h all old hot frames get smaller. No bulk migration needed.

### 8.2 Schema changes

One forward migration `095_daily_budget_state.sql`:
- `CREATE TABLE daily_budget_state` (§6.1).
- Backfill from `daily_size_log` for `thumbnails_bytes`; leave other
  buckets at 0; they'll fill in on next worker tick.

### 8.3 Settings migration

Rename `daily_size_budget_mb` → `legacy_thumb_budget_mb` via a
compatibility shim in pydantic (read either env var; new code reads only
the new name). Add `daily_budget_mb=25.0`. The pydantic `extra="ignore"`
at `config.py:20` means stale env vars won't blow up.

### 8.4 Rollout order

1. Land the daily_budget_state table + projection logic (no behaviour
   change — pure measurement).
2. Wire the throttle states (capture_loop + audio_worker honour level).
3. Ship the dep cuts behind `audio_vad_backend="webrtcvad"` and
   `audio_preferred_codec="opus"` defaults.
4. Flip default `capture_interval_seconds` 5 → 8, `thumbnail_quality`
   45 → 35, `thumbnail_max_width` 900 → 640,
   `dedup_hamming_threshold` 4 → 8, `smart_min_gap_seconds` 180 → 300,
   `tier_warm_after_days` 7 → 1, `audio_preferred_codec` "encodec" →
   "opus", `audio_target_bitrate` 1500 → 6000, `audio_vad_threshold`
   0.5 → 0.6.
5. Implement event log (§5) as its own feature behind
   `event_log_enabled=True` default.
6. Add `capture_profile` setting (§5.3) with `pixel_first` default.

Each step is one PR. Each can ship independently — the budget enforcer
doesn't depend on the dep cuts and vice versa.

---

## 9. Honest limits

**This cap is feasible for the median user. It is not feasible for every
user.** Edge cases where 25 MB/day is impossible without total feature
loss:

### 9.1 Heavy talker (>6 h voiced/day)

At 6 h voiced × 6 kbps = 16.2 MB just for audio. The entire 25 MB
budget collapses if the user is a streamer, podcaster, or all-day
meeting attendee.

Achievable for that subsystem: **degrade Opus to 3 kbps for the day with
a UI warning**. At 3 kbps narrowband 8 kHz, voice is still intelligible
but degrades audibly — quality similar to a bad GSM call. 6 h × 3 kbps =
8.1 MB. Combine with strict-mode throttle on screens and the rest of
the day fits. Set this as the **throttle level 3 "emergency" audio
action** rather than a default.

If the user genuinely wants high-quality 6+ h/day audio: the only honest
answer is "raise the cap". Suggest `daily_budget_mb=40` for that
profile.

### 9.2 Multi-monitor 4K

`multi_monitor=True` + 4 K monitors balloon the WebP byte cost ~3×
because the pre-resize image holds 4× the pixels and LANCZOS down-sample
to 640 px doesn't fully reclaim that cost (still hits 50-80 KB per WebP
vs. 30 KB on 1440p single).

Achievable: drop `thumbnail_max_width` to 480 px in multi-monitor mode,
quality to 30. Brings per-shot back to ~30 KB. **This is a real
quality hit** — small text on those thumbnails becomes hard to read.
Honest trade.

### 9.3 Visually frenetic days (lots of new apps / tabs / windows)

Dedup rate craters when the user is actually doing varied work. A day
of "open 40 different docs, write 10 emails, fly through Notion pages"
might land at 50 % dedup instead of 85 %. Screens bucket overflows
proportionally.

Achievable: the throttle (§6) kicks the system into event-first mode
after 90 % of the budget is reached; user loses pixel coverage for the
last ~3 hours but the event log preserves the timeline. Reconstruction
fidelity drops but is not zero.

### 9.4 OCR + embeddings simultaneously on

Each is allowed 1.5 MB/day in the §2 split. If the user has BOTH on AND
captures a lot of text-heavy frames AND has long OCR strings (PDFs,
spreadsheets), they can blow through 3 MB in either bucket.

Achievable: cap OCR text per row at 4 KB stored (truncate to first ~600
chars), and skip embedding rows where `len(ocr_text) > 4096`. Cuts
information density but holds the cap.

### 9.5 What we are NOT promising

- Lossless audio. 4 kbps Opus is voice-call quality; do not market this
  as "perfect transcription audio". Whisper "small" on 6 kbps narrowband
  loses ~5 % word-error-rate vs. the same speech at 16 kHz wideband.
- Pixel-perfect history. With event-first mode and 70 anchor frames/day,
  you can answer "what app was I in at 14:30?" precisely, and "what was
  on the screen at 14:30?" approximately.
- Sub-second event timing. The event log debounces; expect ~1 s
  granularity.

---

## 10. Concrete defaults table

| Setting | Current | Proposed | File:line |
|---------|---------|----------|-----------|
| `capture_interval_seconds` | 5.0 | **8.0** | `config.py:27` |
| `thumbnail_quality` | 45 | **35** | `config.py:28` |
| `thumbnail_max_width` | 900 | **640** | `config.py:29` |
| `dedup_hamming_threshold` | 4 | **8** | `config.py:30` |
| `idle_threshold_seconds` | 300 | 300 (unchanged) | `config.py:32` |
| `smart_thumbnail` | True | True (unchanged) | `config.py:35` |
| `smart_min_gap_seconds` | 180 | **300** | `config.py:36` |
| `daily_size_budget_mb` (legacy) | 4.0 | renamed `legacy_thumb_budget_mb`, gated off when `budget_enforcer_enabled` | `config.py:37` |
| `daily_budget_mb` (new) | — | **25.0** | new |
| `budget_throttle_aggressiveness` | — | **"mild"** | new |
| `budget_enforcer_enabled` | — | **True** | new |
| `tier_warm_after_days` | 7 | **1** | `config.py:38` |
| `tier_cold_after_days` | 30 | 30 (unchanged) | `config.py:39` |
| `tier_warm_thumbnail_width` | 320 | **256** | `config.py:40` |
| `tier_warm_thumbnail_quality` | 30 | **25** | `config.py:41` |
| `adaptive_min_seconds` | 30 | **60** | `config.py:110` |
| `adaptive_max_seconds` | 600 | **900** | `config.py:111` |
| `OPUS_BITRATE_BPS` (code) | 4000 | **6000** | `encode.py:46` |
| `SAMPLE_RATE` (audio capture) | 16000 | **8000** | `capture.py:57` |
| `audio_vad_threshold` | 0.5 | **0.6** | `config.py:217` |
| `audio_preferred_codec` | "encodec" | **"opus"** | `config.py:216` |
| `audio_target_bitrate` | 1500 | **6000** | `config.py:215` |
| `audio_retention_hot_days` | 7 | 7 (unchanged) | `config.py:194` |
| `audio_keep_sample_pct` | 0.05 | 0.05 (unchanged) | `config.py:196` |
| `audio_vad_backend` (new) | — | **"webrtcvad"** | new |
| `audio_transcribe_enabled` (new) | (always on if installed) | **False (opt-in)** | new |
| `event_log_enabled` (new) | — | **True** | new |
| `capture_profile` (new) | — | **"pixel_first"** | new |
| `active_window_only` (new) | — | **False (opt-in)** | new |

End of design doc.
