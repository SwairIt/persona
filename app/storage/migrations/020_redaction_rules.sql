CREATE TABLE IF NOT EXISTS redaction_rule (
    name TEXT PRIMARY KEY,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO redaction_rule (name, pattern, enabled)
VALUES ('email', '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', 1);

INSERT OR IGNORE INTO redaction_rule (name, pattern, enabled)
VALUES ('credit_card', '\b(?:\d[ -]*?){13,19}\b', 1);

INSERT OR IGNORE INTO redaction_rule (name, pattern, enabled)
VALUES ('bearer_token', '(?i)bearer\s+[A-Za-z0-9._\-]+', 1);
