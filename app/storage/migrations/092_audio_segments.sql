-- v1.11 feature 1/3 — speech-only audio capture.
--
-- The audio worker (``app.workers.audio_worker``) records 30 s chunks
-- from the microphone, runs silero-vad to extract speech segments,
-- preprocesses each segment (band-pass + EBU R128), encodes via
-- Encodec (preferred) / Opus / ffmpeg, writes the bytes under
-- ``data/audio/YYYY/MM/DD/{shot_id}.{ext}`` and inserts one row here
-- per segment.
--
-- Privacy posture: ``settings.audio_capture_enabled`` defaults to OFF
-- so this table stays empty until the user explicitly opts in. The
-- worker re-checks the flag at startup.
--
-- Column contract
-- ---------------
--   * ``started_at`` / ``ended_at`` — ISO-8601 UTC timestamps marking
--     the wall-clock bounds of the speech segment.
--   * ``duration_s`` — convenience column for the timeline UI; equals
--     ``julianday(ended_at) - julianday(started_at)`` but precomputed
--     so the per-row dashboard query stays index-only.
--   * ``codec`` — one of ``"encodec"`` / ``"opus"`` / ``"opus_ffmpeg"``
--     / ``"missing_dep"`` (worker writes ``missing_dep`` when none of
--     the encoders are available so telemetry can spot the regression).
--   * ``bitrate`` — nominal bitrate in bits-per-second (1_500 for
--     encodec, 4_000 for opus). NULL when the codec is variable.
--   * ``path`` — filesystem path relative to ``settings.data_dir`` of
--     the encoded segment. The day-view route reads this; the
--     retention worker (feature 2/3) reaps it after the hot window.
--   * ``size_bytes`` — on-disk size at write time. The retention sweep
--     zeroes this to 0 when it deletes the audio.
--   * ``transcript`` — the Whisper transcription, filled in by the
--     transcribe step in feature 2/3. NULL until the transcribe step
--     completes (or stays NULL forever when Whisper isn't installed).
--   * ``locale`` — optional BCP-47 locale hint (``ru`` / ``en-US``)
--     surfaced by Whisper's language detector; useful when the same
--     row is later re-transcribed with a different model.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS audio_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_s REAL NOT NULL,
    codec TEXT NOT NULL,
    bitrate INTEGER,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    transcript TEXT,
    locale TEXT
);

CREATE INDEX IF NOT EXISTS idx_audio_segment_started_at
    ON audio_segment (started_at DESC);

CREATE INDEX IF NOT EXISTS idx_audio_segment_codec
    ON audio_segment (codec);
