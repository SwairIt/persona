-- v1.40 — Audio segment merger: collapse VAD-fragmented utterances.
--
-- Silero-VAD occasionally splits a continuous voice run into 2-3 short
-- segments separated by sub-second silences (breath, soft consonant,
-- mid-sentence pause). Each fragment lands as its own row in
-- ``audio_segment`` (migration 092). The result is a fragmented
-- transcript timeline — a single sentence shows up as three rows in
-- the day-view and search treats each fragment as a separate hit,
-- inflating row counts and making the dashboard noisy.
--
-- v1.40 introduces a logical-merge layer: a periodic worker
-- (``app.workers.audio_merge_worker``) walks recent rows, groups
-- adjacent ones whose inter-segment silence is below
-- ``gap_seconds`` (default 1.0), inserts ONE new ``audio_segment``
-- with the concatenated transcript + summed duration, and then marks
-- the original rows as merged-into the new row via the columns added
-- here. The user keeps the option to revisit the fragments — nothing
-- is deleted — but the canonical, search-facing row is the merged one.
--
-- Column contract
-- ---------------
--   * ``merged_into_id`` — when non-NULL, this row was rolled up into
--     the referenced ``audio_segment.id``. Search / waveform / day-view
--     queries filter ``merged_into_id IS NULL`` so the user only sees
--     canonical rows. The reference is to the SAME table; ON DELETE is
--     intentionally NOT cascaded — deleting the canonical merged row
--     should leave the fragments behind so the operator can re-merge
--     after diagnosing the deletion.
--   * ``merged_at`` — ISO-8601 UTC wall-clock at which the merge
--     happened. NULL on the canonical merged rows themselves; only the
--     fragment rows carry a value. Useful for telemetry ("how stale is
--     the oldest unmerged fragment?") and for a future "undo merge"
--     admin action.
--
-- Both columns are added via ``ALTER TABLE`` so existing audio rows
-- survive with NULLs; the idempotent migration runner (see
-- ``app/storage/db.py``) swallows the "duplicate column" error on a
-- re-run. The index on ``merged_into_id`` keeps the
-- ``merged_into_id = ?`` admin lookups and the
-- ``merged_into_id IS NULL`` search filter cheap.

ALTER TABLE audio_segment ADD COLUMN merged_into_id INTEGER REFERENCES audio_segment(id);
ALTER TABLE audio_segment ADD COLUMN merged_at TEXT;

CREATE INDEX IF NOT EXISTS idx_audio_segment_merged_into
    ON audio_segment(merged_into_id);
