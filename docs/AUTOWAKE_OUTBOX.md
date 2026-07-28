# Owner-only autowake outbox

Autowake turns a validated proactive event into an owner Telegram DM. It is a
durable, at-least-once pipeline; it is not a second chat implementation.

## Safety boundary

- Producers call `AutowakeService.enqueue` with the configured owner id,
  `is_owner=True`, a stable idempotency key, and an explicit `SourceScope`.
- Only `owner_direct`, `owner_private`, and `derived_owner` may be delivered.
  Group, external, secret-classified, group-marked, and secret-like content is
  rejected before a message or outbox body is stored.
- Rejected audit events retain only safe classification metadata. Their caller
  idempotency key and content-derived fingerprint are not persisted.
- `OwnerTelegramGateway` has no target-chat argument. Its implementation must
  resolve the already-paired owner private chat and verify that it is a DM.
- The dispatcher also checks every claimed row against the configured Persona
  owner id before invoking the gateway.

## Delivery semantics

`206_autowake_outbox.sql` records an event, session, assistant message, and
outbox entry in one `BEGIN IMMEDIATE` transaction. Duplicate accepted events
are suppressed by `(owner_user_id, idempotency_key)` and conflicting reuse is
an error.

Before each attempt, policy re-checks:

- the existing weekly `quiet_hours` table and the default 23:00-08:00 window;
- a two-hour cooldown after the most recent successful delivery;
- a four-message owner daily cap.

Rows are atomically leased. Transport failures retry with bounded exponential
backoff, and exhausted attempts become durable `dead` rows. Expired leases are
recovered and counted as failed attempts. Exception text is never persisted or
logged by this slice.

The transport is at-least-once: if Telegram accepts a message and the process
dies before the delivered transition, the lease eventually retries it. The
gateway receives the idempotency key so a future transport adapter can add a
stronger receipt/deduplication layer.

## Composition-root integration

The composition root still has two explicit jobs:

1. Implement `OwnerTelegramGateway` over the existing Telegram bot pairing,
   resolving only the configured owner's private chat.
2. Register `run_autowake_dispatcher(gateway, expected_owner_user_id=...)` in
   the supervised worker registry.

Briefing, reminder, dream, and memory producers should enqueue short,
already-generated owner-safe messages after their own durable work commits.
They must not pass raw chat transcripts, group-derived summaries, environment
values, tokens, or tool output into autowake.
