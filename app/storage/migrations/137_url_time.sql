-- v1.50 — Per-URL (best-effort) time tracking.
--
-- We can't read the URL bar from a screenshot — the window title is all
-- we have.  When the foreground ``app_name`` is one of the four major
-- desktop browsers we treat the window title as a coarse "page label"
-- (the document/tab heading the browser writes in front of " — Google
-- Chrome" / " — Mozilla Firefox" / etc.) and bucket screenshots by it.
-- That is wildly imprecise — pages with the same heading collapse, and
-- two visits to the same URL with different titles split — but it's the
-- best signal a vision-only timeline has and it's still useful for "I
-- spent four hours on YouTube today" style retrospectives.
--
-- Schema notes:
--   day              — ISO ``YYYY-MM-DD`` in the operator's local timezone.
--                      The aggregator uses ``DATE(captured_at)`` on the
--                      screenshots table, which is wall-clock local since
--                      ``captured_at`` is stored as a local ISO timestamp.
--   browser          — the raw ``screenshots.app_name`` ("Google Chrome",
--                      "Firefox", "Safari", "Microsoft Edge"). We keep
--                      it stringly-typed rather than introducing a FK to
--                      a browsers lookup — the set is small and stable
--                      enough that filtering on it in SQL is fine.
--   page_label       — lower-cased, suffix-stripped, truncated to 80
--                      chars by app.url_time_tracker.extract_page_label.
--                      Truncation prevents extremely long titles from
--                      blowing out the UNIQUE index.
--   screen_count     — number of screenshots in the day that mapped to
--                      this (browser, page_label).  Exact, not gap-aware.
--   est_seconds      — ``screen_count * capture_interval_seconds`` at
--                      the moment the row was computed.  Intentionally a
--                      static estimate — we recompute the whole row on
--                      every refresh so drifting the interval just
--                      changes the next aggregate, not historical ones.
--   computed_at      — ISO timestamp of the most recent refresh.  The
--                      worker recomputes the last three days on every
--                      tick so this advances frequently for today and
--                      then stabilises.
--
-- Indices:
--   UNIQUE(day, browser, page_label) — drives the upsert in
--                      app.url_time_tracker.aggregate_day.
--   idx_url_time_day — the dashboard query is always "give me the last
--                      N days", so the date column carries the index.

CREATE TABLE IF NOT EXISTS url_time_aggregate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    browser TEXT NOT NULL,
    page_label TEXT NOT NULL,
    screen_count INTEGER NOT NULL DEFAULT 0,
    est_seconds INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(day, browser, page_label)
);

CREATE INDEX IF NOT EXISTS idx_url_time_day ON url_time_aggregate(day);
