# Dream memory ledger

Nightly reflection is a proposal-only workflow. Model output is never executed
as a memory command.

1. `dream_run` acquires a renewable lease under a user-scoped idempotency key.
2. Light sleep stores every `dream_candidate` and its provenance in
   `dream_evidence`.
3. `DreamPolicy` deterministically checks score, source diversity, memory kind,
   cap, and trusted owner attribution.
4. `SqliteDreamLedger` rechecks critical policy invariants and applies an
   approved change in one short transaction.
5. The exact before/after record is appended to `dream_revision`; lifecycle
   events are appended to `dream_audit`.
6. REM reflection, `dream_report`, processed-message cursor, terminal
   `dream_run` state, completion audit, and graph/embedding projection intents
   commit in the same transaction.
7. A supervised worker processes those intents outside the dream transaction;
   see [Memory projection outbox](MEMORY_PROJECTION_OUTBOX.md).

`dream_evidence`, `dream_revision`, and `dream_audit` are append-only by SQLite
triggers. Telegram group speech, screen OCR, and ambient audio may contribute
context, but none can independently establish a fact about the owner. At least
one explicitly attributed owner-chat record is required.

Pinned memory is immutable to this automated workflow. Update policy rejects a
pinned target, and the adapter repeats that check inside the write transaction.
Manual owner actions in the memory inspector remain outside this automation
policy.

Append-only is an operational invariant, not a reason to retain personal data
against an erasure request. Migration 205 installs a transactional
`users BEFORE DELETE` privacy trigger. It activates a per-user purge guard,
deletes ledger children in dependency order, and lets the normal user deletion
continue. An `AFTER DELETE` trigger removes the guard. Direct ledger deletion
without this user-erasure context remains blocked. If any purge step fails, the
outer user deletion and all ledger deletions roll back together.

Operational inspection:

```sql
SELECT id, user_id, status, attempt_count, input_cursor, safe_cursor,
       candidates_count, applied_count, rejected_count, error
FROM dream_run
ORDER BY id DESC
LIMIT 20;

SELECT c.id, c.status, c.policy_reason, c.text, COUNT(e.id) AS evidence_count
FROM dream_candidate c
LEFT JOIN dream_evidence e ON e.candidate_id = c.id
WHERE c.run_id = ?
GROUP BY c.id
ORDER BY c.id;
```

Current deliberate limitation: automatic sleep creates `add` proposals only.
The domain and adapter support a tightly checked update, but no generative
component selects an update target yet. Automatic delete is always rejected.
Graph projection and post-apply embeddings consume eligible applied revisions
through the separate idempotent outbox; provider I/O is never performed inside
the memory completion transaction.
