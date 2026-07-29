# Memory projection outbox

Migration 210 adds an evidence-linked, owner-scoped outbox for derived graph
edges and memory-revision embeddings.

## Commit and delivery contract

- `SqliteDreamLedger.complete_run()` inserts projection intents in the same
  SQLite transaction as the dream report, cursor, terminal run state, and
  completion audit. A crash cannot commit a memory revision while losing its
  projection intent.
- The worker claims one due item in a short transaction, releases SQLite,
  performs graph extraction or embedding I/O, then opens a second short
  transaction to store the result and mark the item done.
- `(dream_revision_id, projection_kind, projector_version)` is unique. Graph
  triples are additionally linked by `(dream_revision_id, triple_hash)`, so a
  retry cannot strengthen the same edge twice. Embeddings upsert by revision.
- Expired leases are reclaimed. Failures use bounded exponential retry and
  become `dead` after `max_attempts`.

The outbox references the exact `dream_revision`, `user_memory`, and trusted
`dream_evidence` rows. Claim and completion both re-read the source and apply a
fail-closed policy. Rejected candidates, group evidence, secrets (including
Russian `пароль`/`токен`), pinned memory, invalidated memory, unsupported
revision actions, changed content, and non-owner rows are never projected.

The adapters intentionally reuse the existing graph extractor/store and
embedding provider. Optional-provider failures are visible as
`degraded`/`unavailable`; they are not silently reported as successful. The
embedding source of truth is a regular BLOB table, so lack of the optional
`sqlite-vec` extension cannot lose the revision embedding.

User deletion cascades outbox/evidence/embedding/revision links. Migration 210
also removes legacy graph edges and entities in the same user-deletion
transaction.

## Runtime and operations

`memory-projection-worker` runs in both `lean` and `full` profiles under the
background supervisor. Its heartbeat key is `memory-projection`.

Owner-only `GET /api/health/full` includes:

- `memory_projection.counts` by outbox status;
- `memory_projection.oldest_active_at`;
- graph/embedding capability status, counters, and sanitized detail code;
- the latest worker heartbeat;
- `queue_depths.memory_projection` from the shared queue diagnostics.

Useful inspection queries:

```sql
SELECT id, projection_kind, status, attempts, max_attempts, due_at,
       last_error_code, dream_revision_id, memory_id
FROM memory_projection_outbox
ORDER BY id DESC
LIMIT 50;

SELECT name, status, detail_code, successes, failures, checked_at
FROM memory_projection_capability
ORDER BY name;
```

`dead` items remain inspectable. Repair the provider/capability first; replay
should be an explicit operator action rather than an automatic infinite loop.
