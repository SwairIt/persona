-- v0.36 — append-only audit log for security-sensitive admin actions.
--
-- Captures the *who/what/when* for every privileged operation Persona
-- exposes (bulk-delete, token issuance + revocation, vault reads /
-- writes / deletes, settings changes, password updates, …). The table
-- is deliberately schemaless on the payload side: ``detail`` is a free
-- form TEXT column rather than JSON because most callers only want a
-- one-line human-readable note and we don't want to force them through
-- a serialisation step for that.
--
-- Design notes
-- ------------
-- * ``ts`` is wall-clock ISO-8601 derived from SQLite's ``datetime('now')``
--   so a corrupt clock can be spotted in the same string format the
--   rest of Persona already uses.
-- * ``actor`` is ``NULL``-able — many admin actions run from the
--   single-user owner session and we don't have a separate identity to
--   record. Future multi-user installs can populate it with a username.
-- * ``success`` is INTEGER (0/1) so the log captures failed attempts
--   (e.g. wrong vault password) alongside successful ones — failures
--   are the more interesting signal in a security review.
-- * ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``
--   keep the migration idempotent across re-runs of ``init_database``.
-- * **Never store secret values.** Callers MUST log only key names,
--   ids, counts and other non-sensitive metadata. The plaintext of a
--   vault row or the raw bytes of an API token never enter this table.

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    action TEXT NOT NULL,
    actor TEXT,
    target TEXT,
    detail TEXT,
    success INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts DESC);
