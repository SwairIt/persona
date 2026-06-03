-- v0.54 — per-day storage-savings counter.
--
-- Three independent housekeeping passes already trim Persona's on-disk
-- footprint: :func:`app.dedup.phash.find_or_create_dedup_group` skips
-- writing a screenshot row when a near-duplicate is detected,
-- :func:`app.thumb_dedup.scan_and_dedup` collapses byte-identical
-- thumbnail files onto a single canonical copy, and
-- :func:`app.recycle.purge_expired` hard-deletes soft-deleted rows once
-- their retention window expires. None of these emit a usable timeline —
-- the operator only sees ad-hoc log lines and the immediate scan tally.
--
-- This table is the per-day journal so the new ``/stats/storage-savings``
-- page can render a line chart of bytes reclaimed over time. One row
-- per UTC day, keyed by ``day`` (``YYYY-MM-DD``); the three savings
-- sources each get their own column so the chart can stack them and the
-- table can credit each housekeeping pass independently.
--
-- ``bytes_saved`` is the rolling grand total written by every recorder
-- (``record_dedup_hit`` adds its bytes estimate, ``record_thumb_dedup``
-- adds the real reclaimed bytes, ``record_retention_freed`` adds the
-- bytes that hard-delete freed). The per-source columns let us slice
-- the same total without recomputing — handy for the chart legend.
--
-- ``dedup_hits`` is a count (not bytes) because the dedup pass works
-- pre-write: there is no on-disk file to measure, only the would-be
-- footprint of the skipped screenshot. The chart reports the bytes
-- estimate as part of ``bytes_saved`` while ``dedup_hits`` lets the
-- table column credit the actual number of duplicates avoided.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent across re-runs.

CREATE TABLE IF NOT EXISTS storage_saving (
    day TEXT PRIMARY KEY,
    bytes_saved INTEGER NOT NULL DEFAULT 0,
    dedup_hits INTEGER NOT NULL DEFAULT 0,
    thumb_dedup_bytes INTEGER NOT NULL DEFAULT 0,
    retention_freed_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_storage_saving_day ON storage_saving(day);
