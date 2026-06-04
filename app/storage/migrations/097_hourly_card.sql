-- v1.14 — tier 1 memory: per-hour summary cards.
--
-- Each card distills one full hour of capture (screens + OCR + audio
-- transcript) into a ~10 KB markdown blob plus structured columns. The
-- hourly_card_worker writes one row per completed hour; the /ask
-- endpoint pulls top-k cards by FTS5 + recency.
--
-- "Heuristic-first" — the first version computes everything (apps,
-- duration, top words, transcript snippets) deterministically from
-- existing tables. LLM enrichment is an opt-in column added in a
-- follow-up migration.

CREATE TABLE IF NOT EXISTS hourly_card (
    hour_start TEXT PRIMARY KEY,            -- ISO UTC, HH:00:00
    hour_end TEXT NOT NULL,                 -- ISO UTC, HH:59:59
    summary TEXT NOT NULL,                  -- markdown block
    apps_json TEXT,                         -- top apps + minutes
    screen_count INTEGER NOT NULL DEFAULT 0,
    audio_seconds INTEGER NOT NULL DEFAULT 0,
    top_words TEXT,                         -- comma-separated OCR keywords
    transcript_excerpt TEXT,                -- first ~500 chars of audio transcript
    llm_enriched INTEGER NOT NULL DEFAULT 0,-- 1 once LLM has added narrative
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_hourly_card_created
    ON hourly_card(created_at);

-- FTS5 mirror for fast Q&A retrieval over card summary + transcript.
CREATE VIRTUAL TABLE IF NOT EXISTS hourly_card_fts USING fts5(
    summary,
    transcript_excerpt,
    top_words,
    content='hourly_card',
    content_rowid='rowid'
);

-- Keep the FTS5 mirror in sync via the standard trigger pattern.
CREATE TRIGGER IF NOT EXISTS hourly_card_ai AFTER INSERT ON hourly_card BEGIN
    INSERT INTO hourly_card_fts(rowid, summary, transcript_excerpt, top_words)
    VALUES (new.rowid, new.summary, new.transcript_excerpt, new.top_words);
END;

CREATE TRIGGER IF NOT EXISTS hourly_card_au AFTER UPDATE ON hourly_card BEGIN
    INSERT INTO hourly_card_fts(hourly_card_fts, rowid, summary, transcript_excerpt, top_words)
    VALUES('delete', old.rowid, old.summary, old.transcript_excerpt, old.top_words);
    INSERT INTO hourly_card_fts(rowid, summary, transcript_excerpt, top_words)
    VALUES (new.rowid, new.summary, new.transcript_excerpt, new.top_words);
END;

CREATE TRIGGER IF NOT EXISTS hourly_card_ad AFTER DELETE ON hourly_card BEGIN
    INSERT INTO hourly_card_fts(hourly_card_fts, rowid, summary, transcript_excerpt, top_words)
    VALUES('delete', old.rowid, old.summary, old.transcript_excerpt, old.top_words);
END;
