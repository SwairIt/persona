# Persona migration ledger runbook

The authoritative migration implementation is
`app/storage/migration_runner.py`. Application processes call it through
`app.storage.db.init_database()`.

## Invariants

- Migration files are immutable after they have been applied.
- A filename starts with a unique three-digit order, for example
  `204_example.sql`.
- The SHA-256 checksum is calculated after normalising line endings, so the same
  Git revision has the same checksum on Windows and Linux.
- A migration and its `schema_migration(status='applied')` row commit in the
  same `BEGIN IMMEDIATE` transaction.
- A failed batch is rolled back. The failing migration is then recorded with
  `status='failed'` and `applied_at=NULL`.
- Any failed row or checksum/order drift blocks application startup.
- Concurrent application starts elect one migrator through SQLite's writer
  lock. Other processes wait and then verify the completed ledger.
- Migrations 164, 179, 198 and 199 are never replayed on an ordinary startup.

The first deployment to a current legacy database (a database created by the
old replay runner) builds the release schema in a disposable in-memory database
and compares every canonical table/view, column, foreign key, explicit index
and trigger before creating baseline rows. Extra optional sqlite-vec shadow
objects are ignored, but every extension-free canonical object must match. If
the full structural fingerprint cannot be proven, startup fails closed instead
of replaying destructive or operator-visible migrations.

Optional sqlite-vec tables are tracked separately in `schema_capability`.
Installing the extension later creates the missing vec0 tables without
replaying migrations 186 or 190. An unavailable or failed optional capability
is observable in that table but does not roll back or block the core schema.

## Before production rollout

1. Stop additional deploys and take a verified copy/backup of `persona.db`,
   `persona.db-wal` and `persona.db-shm` using the existing backup procedure.
   Do not copy a live SQLite database with ordinary filesystem copy unless it
   has first been checkpointed or the process is stopped.
2. Rehearse startup against a restored copy with the exact release commit.
3. Inspect the result:

```sql
SELECT migration_order, name, status, is_baseline, duration_ms, applied_at
FROM schema_migration
ORDER BY migration_order;
```

4. Start one application process first. Confirm that every row is `applied` and
   the latest migration matches the release. Then start the remaining
   processes.
5. A second startup should execute zero migration bodies. It should normally
   complete the migration check in less than 250 ms on the production host.

## Adding a migration

1. Never edit an already deployed migration.
2. Add the next ordered SQL file. Use `IF NOT EXISTS` where it preserves the
   intended invariant.
3. Keep data backfills bounded and documented. Network/LLM calls are forbidden.
4. For a table rebuild, document data-copy validation and rollback implications.
5. Add an adapter/migration test for both the pre-migration and fresh schema.
6. Test two simultaneous `init_database()` calls.

The runner only tolerates two legacy compatibility cases:

- duplicate columns from `ALTER TABLE ... ADD COLUMN`;
- an existing index from `CREATE [UNIQUE] INDEX` that historically omitted
  `IF NOT EXISTS`.

`no such column`, missing tables, arbitrary `already exists`, syntax errors and
unknown virtual-table modules are not swallowed.

## Failure recovery

Inspect the failure without exposing secrets:

```sql
SELECT migration_order, name, checksum, status, started_at, finished_at, error
FROM schema_migration
WHERE status = 'failed';
```

The migration body has already been rolled back. Do not simply mark the row
`applied`.

1. Keep the application stopped.
2. Verify the database with `PRAGMA integrity_check;`.
3. Fix the migration in the release if it has never been applied anywhere, or
   restore the exact migration file if the failure was caused by a damaged
   checkout.
4. Back up the database again.
5. In a maintenance session, remove only the reviewed failed row:

```sql
BEGIN IMMEDIATE;
DELETE FROM schema_migration
WHERE name = 'NNN_name.sql' AND status = 'failed' AND applied_at IS NULL;
COMMIT;
```

6. Start one process and verify the ledger before scaling out.

For `__legacy_baseline__.sql`, deletion alone is not a repair. Its error lists
the mismatching structural objects or facets (`table.columns`,
`table.foreign_keys`, `table.indexes`, or `table.triggers`). Restore/upgrade a
copy using the previous compatible application until it reaches the current
schema and validate it.
Only then remove the synthetic failed row as described above. If no applied
rows remain, the runner repeats the complete structural comparison and creates
a baseline; it never replays historical migrations merely because the empty
ledger tables still exist.

## Checksum drift

If startup reports checksum drift:

- restore the migration file byte-for-byte from the commit that originally
  applied it;
- put any corrective SQL in a new migration;
- never update the stored checksum to silence the error.

Line-ending-only changes do not cause drift.

## Application rollback

Schema migrations are forward changes. Before rolling application code back,
verify that the older application supports the current schema.

Important: releases predating the ledger replay every SQL file and therefore
must not be started directly against a ledger-managed production database.
Use a release containing this runner, or restore the pre-deploy database backup
and its matching application commit.

If a migration is additive and the old code tolerates the extra schema, rolling
back application code while keeping the database may be safe. Destructive or
semantic migrations require restoring the verified backup. Never manually drop
new columns/tables in production as an improvised rollback.
