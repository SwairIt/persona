-- 177_db_hardening.sql — Phase 0 foundation: indexes on existing hot paths.
-- Additive + idempotent (CREATE INDEX IF NOT EXISTS). No table changes here;
-- per-feature tables/indexes ship in their own later migrations.

-- Pinned messages are scanned on every chat turn to re-inject context.
-- Partial index keeps it tiny (only the few pinned rows).
CREATE INDEX IF NOT EXISTS idx_chat_message_pinned
    ON chat_message(session_id) WHERE is_pinned = 1;

-- /audit filters by action / actor via LIKE — back them with indexes.
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);

-- Expired-session cleanup + verify_session lookups.
CREATE INDEX IF NOT EXISTS idx_auth_session_expires ON auth_session(expires_at);

-- Voice TTS queue is polled by the agent ordered by id within status.
CREATE INDEX IF NOT EXISTS idx_voice_tts_session ON voice_tts(session_id, id);
