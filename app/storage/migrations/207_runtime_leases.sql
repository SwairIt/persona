-- Cross-process singleton leases for workers that must never run twice.
CREATE TABLE IF NOT EXISTS runtime_lease (
    name                TEXT PRIMARY KEY,
    holder_id           TEXT NOT NULL,
    lease_until         TEXT NOT NULL,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_runtime_lease_expiry
    ON runtime_lease(lease_until);
