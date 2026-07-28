-- A browser worker has a capability-scoped credential and must never reuse
-- the LLM worker token.  Empty means disabled until the explicit provisioner
-- creates a random token and stores only its SHA-256 digest.
INSERT INTO kv_settings(key, value, updated_at)
VALUES ('remote_browser_worker_token_hash', '', datetime('now'))
ON CONFLICT(key) DO NOTHING;
