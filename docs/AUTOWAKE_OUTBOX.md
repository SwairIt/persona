# Privacy-scoped autowake outbox

Autowake turns a validated proactive event into an owner Telegram DM or an
explicitly allowlisted Telegram group message. It is a durable, at-least-once
pipeline; it is not a second chat implementation.

## Safety boundary

- Producers call `AutowakeService.enqueue` with the configured owner id,
  `is_owner=True`, a stable idempotency key, and an explicit `SourceScope`.
- `owner_direct`, `owner_private`, and `derived_owner` may target only the
  owner DM. `group` may target only the same explicitly opted-in group and
  requires `telegram_group` provenance.
- Rejected audit events retain only safe classification metadata. Their caller
  idempotency key and content-derived fingerprint are not persisted.
- Owner delivery resolves the paired private chat. Group delivery carries a
  negative chat id and re-checks the current Telegram allowlist at send time,
  so `/deny_here` closes delivery even for an already queued row (unless that
  chat is deliberately pinned in the static environment allowlist).
- The dispatcher also checks every claimed row against the configured Persona
  owner id before invoking the gateway.

## Delivery semantics

`206_autowake_outbox.sql` records an event, session, assistant message, and
outbox entry in one `BEGIN IMMEDIATE` transaction. Duplicate accepted events
are suppressed by `(owner_user_id, idempotency_key)` and conflicting reuse is
an error. `215_autowake_group_delivery.sql` adds the constrained group target
without changing existing owner-DM rows.

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

## Persona impulse producer

`persona-impulse-producer` is supervised in both full and lean profiles. Every
five minutes it:

- checks quiet hours, the two-hour cooldown, and the four-message daily cap
  before reading context or calling the LLM;
- selects one recent Telegram conversation (owner DM up to 24 hours old, or
  one allowlisted active group up to six hours old);
- gives the model only that conversation's bounded excerpts and no tools;
- defaults to `SILENT`, truncates output to 600 characters, and deduplicates
  one intent per destination per 30-minute slot;
- calls the LLM outside database transactions and persists only a successful
  delivery intent through the existing durable outbox.

Set `kv_settings.persona_impulse_enabled` to `0` to disable generation. An
offline LLM writes no failure marker; the supervised producer retries on its
next cadence. Group context is never combined with owner memory, other chats,
screen activity, secrets, or tools.
