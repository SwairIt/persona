-- v0.36 per-day TL;DR auto-summary cache.
-- Short one-paragraph summary (max 30 words) generated lazily via BYO LLM
-- and cached per calendar day. Surfaces inline on /timeline/{day} and
-- /digest list — never blocks render, populated via JS/HTMX fetch.

CREATE TABLE IF NOT EXISTS day_tldr (
    day TEXT PRIMARY KEY,           -- YYYY-MM-DD
    tldr TEXT NOT NULL,
    provider TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
