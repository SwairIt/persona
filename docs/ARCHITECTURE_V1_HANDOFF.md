# Persona architecture v1 handoff

Status: implementation complete for the first refactoring slice. Package
version: `2.22.0`.

This document records the work completed by the root Codex agent and its
parallel agents so the next session can continue without reconstructing the
entire thread.

## What is implemented

- Owner-only private surface remains enforced; the Telegram channel is bound
  to the configured Persona owner.
- Web and Telegram simple chat use the shared application-level
  `ConversationService` instead of duplicating the complete conversation
  workflow.
- Application startup uses an explicit app factory/lifespan and declarative,
  supervised worker registry. The safe default profile is `lean`; the full
  legacy worker fleet requires `PERSONA_RUNTIME_PROFILE=full`.
- Worker shutdown is bounded per worker and failures/restarts are observable
  through the owner health endpoint.
- Telegram runs inside the supervised lifecycle, has a singleton renewable
  lease, a durable update inbox, and suppresses replayed DB/LLM turns.
- Autowake has a durable owner-only outbox, quiet hours, cooldown, daily cap,
  retries, leases and privacy provenance checks. The morning briefing is its
  first production producer.
- Dream processing is proposal-only and evidence-linked. Candidate promotion,
  cursor movement, report, reflection and audit completion are transactional.
  Pinned memory and Telegram group provenance fail closed. Account privacy
  erasure has a tested transactional purge path without weakening normal
  append-only protection.
- Remote browser execution is outbound-only from the user's PC. It supports a
  small typed Playwright command set, persistent local profile, bounded
  results, prompt redaction and server-side job leases.
- LLM and browser workers use independent scoped tokens. Browser network
  policy is enforced for opens, clicks, redirects, subresources and
  WebSockets; private/reserved networks are blocked.
- The Windows bootstrap installs/updates Python dependencies, Playwright
  Chromium and Ollama models; creates separate per-user scheduled tasks for
  LLM and browser workers; preserves/rolls back existing tasks; and stores
  credentials in a current-user-only ACL file.
- SQLite migrations are transactional and checksum-ledgered. Legacy production
  databases can be structurally verified at the reviewed pre-ledger boundary
  (`203`), baselined, and upgraded by applying only `204+`. Fingerprints cover
  columns, foreign keys, explicit indexes including predicates, views and
  trigger bodies. Optional sqlite-vec failure is recorded as degraded
  capability rather than corrupting the core migration ledger.
- Architecture tests enforce clean domain/application imports, duplicate and
  unreachable routes, a route-count budget, a reviewed route-to-database debt
  baseline and cold-import behavior.

## Parallel-agent ownership

- Conversation/lifecycle agent: shared chat use case, legacy adapter,
  supervised startup registry and clean-layer gates.
- Memory/dream agent: durable dream ledger, proposal policy, atomic completion,
  pinned/provenance safety and privacy erasure.
- Autowake/Telegram agent: durable proactive outbox, owner-DM gateway,
  Telegram lease/inbox idempotency and briefing producer.
- Remote-browser agent: typed browser job protocol, PC worker, scoped token,
  navigation policy, prompt redaction and Windows bootstrap integration.
- Root agent: integration across `main.py`, browser manager/MCP, health,
  migration compatibility/fingerprints, safe-default runtime, versioning,
  combined review and release.

## Deliberate v1 limitations / next work

- Advanced web tool modes still use a legacy path; only the simple chat path is
  fully unified through `ConversationService`.
- Telegram delivery cannot be exactly-once because Telegram Bot API has no
  idempotency receipt. Replayed DB/LLM work is suppressed; a crash at the
  external-send boundary may lose or repeat only the outbound reply.
- Dream generation currently emits safe `add` proposals. `update` is supported
  by policy/repository but target selection is not enabled; automatic `delete`
  remains rejected.
- Dream report autowake has a reusable producer helper but is not connected
  until the completed dream's entire evidence set can be asserted
  owner-private. Briefing autowake is connected.
- Graph/embedding projection should become a separate evidence-linked outbox;
  it is intentionally not performed inside the memory transaction.
- The route-to-database architecture debt baseline still contains legacy
  direct SQL routes. Reduce it monotonically instead of broad rewrites.
- The browser PC needs the new independent
  `PERSONA_BROWSER_WORKER_TOKEN` and a bootstrap rerun before remote browser
  presence can become online.

## Operational continuation

1. Read `CLAUDE.md`, this file, `docs/MIGRATIONS_RUNBOOK.md`,
   `docs/DREAM_LEDGER.md`, `docs/AUTOWAKE_OUTBOX.md` and
   `docs/REMOTE_BROWSER_WORKER.md`.
2. Preserve unrelated local UI/Copilot work in the dirty worktree.
3. Before the first `2.22.0` process restart, create and verify a SQLite backup.
4. Provision independent worker tokens locally with
   `ops/provision_llm_worker_token.py`; never print tokens in logs or chat.
5. Restart Persona in lean mode, then verify `/healthz`, owner-only
   `/api/health/full`, Telegram worker lease, a real LLM job and browser worker
   presence after the PC bootstrap rerun.

No credentials or personal message content are stored in this handoff.
