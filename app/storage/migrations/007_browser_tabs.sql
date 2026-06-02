-- v0.9 — browser-extension tab log.

CREATE TABLE IF NOT EXISTS browser_tabs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    inserted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_browser_tabs_captured ON browser_tabs(captured_at);
CREATE INDEX IF NOT EXISTS idx_browser_tabs_domain ON browser_tabs(domain);
