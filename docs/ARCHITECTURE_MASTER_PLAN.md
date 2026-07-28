# Persona Architecture Master Plan

Status: active
Approach: incremental replacement, no big-bang rewrite
Primary goal: make feature delivery predictable without losing existing product behavior

## 1. Executive decision

Persona has outgrown its feature-first architecture. The product is valuable and
many individual subsystems are well engineered, but HTTP, orchestration, domain
rules and SQLite access are coupled closely enough that changes are increasingly
expensive and runtime failures are difficult to localize.

The repair strategy is a strangler migration:

1. Stop active data-loss, lock-contention and access-control risks.
2. Add executable architecture boundaries before moving code.
3. Extract one vertical slice at a time behind stable ports.
4. Keep old routes and schemas operational until parity tests pass.
5. Delete obsolete paths only after production observation and rollback windows.

A full rewrite is explicitly rejected. Existing behavior, data and tests are assets.

## 2. Evidence baseline

Measured on the repository before this plan:

| Signal | Baseline |
|---|---:|
| Python modules scanned | 868 |
| Python LOC | about 176,200 |
| FastAPI routes | 1,078 |
| Route modules | about 404 |
| SQL migrations | 203 |
| Functions over 100 lines | 90 |
| Route modules directly opening DB connections | about 231 |
| `except Exception` occurrences | about 536 |
| Cold `import app.web.main` | about 11.6 s |
| `create_app()` after import | about 1.5 s |
| Collected tests | 663 |
| Duplicate method/path pairs | 3 |

The worst orchestration hotspot is `api_send_stream` (about 793 lines). The LLM
worker queue previously opened write transactions approximately 3.3 times per
second while idle and the response consumer opened two fresh SQLite connections
every 40 ms while waiting.

These numbers are baselines, not permanent architecture requirements. Every phase
below has a measurable exit condition.

## 3. Problem catalogue and severity

### P0 — correctness, security and availability

1. **LLM worker lock amplification**
   - Empty long-poll repeatedly starts `BEGIN IMMEDIATE`.
   - Response polling opens many short-lived SQLite connections.
   - Logs contain `disk I/O error` on this path.
   - A worker that disappears leaves `streaming` jobs indefinitely.
   - Jobs and chunks have no retention cleanup.
   - Concurrent polls with the same `worker_id` can select the wrong claimed row.

2. **Migration replay is not a migration system**
   - All migration files run on every process start.
   - Some migrations rebuild FTS/table structures or rewrite configuration.
   - There is no applied-migration ledger, checksum or failure state.
   - Broad `"no such column"` / `"already exists"` suppression can hide defects.
   - The custom SQL splitter is unsafe for future triggers and string literals.

3. **Production access policy is not an explicit application invariant**
   - Public, authenticated and owner-only surfaces are distributed across route
     code and middleware exceptions.
   - The desired temporary policy is owner-only internal access; this needs one
     centrally tested gate and an explicit public allowlist.

4. **Runtime stalls have insufficient causal telemetry**
   - Watchdog reports repeated non-response periods, including lean mode.
   - There are no request-loop lag, DB-lock wait or per-worker queue-delay SLOs.
   - `/healthz` is documented/allowlisted but absent.

5. **Routing is ambiguous**
   - Duplicate GET handlers exist for `/changelog`, `/features`,
     `/help/shortcuts`.
   - FastAPI uses first-match behavior, leaving handlers unreachable.

6. **In-progress AI Everywhere integration is contract-incomplete**
   - New route modules are not registered.
   - Translation keys are missing.
   - `page_summary` versus `summary` mode mismatch.
   - Done-event structure differs between backend and frontend.
   - Calendar preview/create may invoke the LLM twice with inconsistent output.
   - This work must be completed or feature-flagged before architecture moves.

### P1 — structural debt that blocks safe development

1. **Missing dependency rule**
   - Web routes import DB connections directly.
   - Domain/application behavior depends on SQLite representation.
   - Templates and SSE protocol details leak into use cases.

2. **Oversized composition root**
   - `app.web.main` owns router imports, middleware, startup/shutdown and workers.
   - Importing one web application eagerly imports most of the product.

3. **God use cases**
   - Chat streaming mixes authorization, recall, prompt assembly, quota,
     provider calls, tool execution, persistence and SSE serialization.
   - Other complex endpoints follow the same pattern.

4. **Service locator and global mutable state**
   - KV lookups and globals make dependencies implicit.
   - Tests must monkeypatch modules rather than construct use cases.
   - Lifecycle ownership of clients, schedulers and caches is unclear.

5. **Background worker sprawl**
   - Many workers start from one lifespan path.
   - Enablement, cadence, resource class, ownership and health are not described
     by a single registry.
   - Idle background work is difficult to budget.

6. **Tenant isolation is convention-based**
   - Many SQL calls receive `user_id`, but the invariant is not enforced at the
     repository boundary.
   - System jobs use `user_id=0`, mixing system identity with tenant identity.

7. **Error policy is inconsistent**
   - Broad catches are common and frequently turn defects into silent fallbacks.
   - Domain errors, transient infrastructure errors and programmer errors are not
     represented by separate types.

8. **Version and product metadata drift**
   - Runtime/service-worker version and packaging/API metadata disagree.
   - README endpoint/worker counts and product model are stale.

### P2 — scale, performance and maintainability

1. **SQLite connection churn**
   - Each repository function commonly creates/configures a new connection.
   - PRAGMAs and extension loading occur repeatedly.
   - There is no explicit read/write gateway with observability.

2. **Schema ownership is unclear**
   - Feature modules do not own migration/repository contracts explicitly.
   - Cross-feature queries are common.
   - Queue chunks lack protocol-level idempotency constraints.

3. **Frontend runtime cost**
   - Tailwind runtime compiler is loaded globally.
   - Many inline scripts/styles prevent clear CSP and code splitting.
   - Image lazy-loading and explicit dimensions are uncommon.
   - Landing page depends on Google Fonts despite strong local/privacy claims.

4. **Route surface is excessively broad**
   - Similar endpoints and narrow feature pages create shallow, duplicated code.
   - API/HTML command/query contracts are inconsistent.

5. **Testing is broad but not architecture-directed**
   - No dependency-boundary tests.
   - No automatic duplicate-route test.
   - No startup budget or idle-write regression test.
   - Full suite duration/reliability is not an enforced CI signal.

6. **Observability does not follow a common event model**
   - Logs exist, but correlation IDs, use-case names, tenant-safe context,
     duration and failure class are inconsistent.

### P3 — polish and long-term quality

1. Documentation and current behavior diverge.
2. Dead routes, unused imports and obsolete compatibility branches accumulate.
3. Accessibility, asset budgets and CSP are not enforced automatically.
4. Public API versioning/deprecation rules are implicit.
5. Recovery drills, backup restore verification and capacity tests are manual.

## 4. Target architecture

The target is pragmatic Clean Architecture, not directory theatre.

```text
app/
  domains/
    chat/
      entities.py
      policies.py
      errors.py
      ports.py
    memory/
    identity/
    billing/
    automation/
  application/
    chat/
      send_message.py
      stream_response.py
      dto.py
    memory/
    automation/
  adapters/
    sqlite/
      chat_repository.py
      memory_repository.py
      unit_of_work.py
      migrations/
    llm/
    telegram/
    filesystem/
  entrypoints/
    web/
      routes/
      presenters/
      dependencies.py
    workers/
    cli/
  bootstrap/
    container.py
    router_registry.py
    worker_registry.py
    lifespan.py
```

Existing directories stay in place during migration. New slices use the target
boundaries; old modules become adapters and are removed gradually.

### Dependency rule

Allowed:

```text
entrypoints -> application -> domains
adapters --------------------> domains ports
bootstrap -> entrypoints + application + adapters
```

Forbidden:

- `domains` importing FastAPI, Jinja, aiosqlite, HTTP clients or app settings.
- `application` importing FastAPI/Jinja or concrete SQLite repositories.
- route modules importing `get_connection` or `write_transaction`.
- repositories returning FastAPI responses or template objects.
- background workers calling route functions.

### Layer responsibilities

**Domain**

- Entities, value objects, policies and invariants.
- Typed domain errors.
- No I/O and no framework imports.

**Application**

- One use-case object/function per user intent.
- Transaction boundary and port orchestration.
- Input/output DTOs that do not expose DB rows.
- Authorization intent (`Actor`, `Owner`, `TenantId`) explicit in input.

**Ports**

- Repository protocols.
- LLM gateway, clock, event publisher, job queue, audit sink.
- Narrow interfaces based on use cases, not database tables.

**Adapters**

- SQLite SQL, provider SDKs, Telegram, filesystem and HTTP implementations.
- Translate infrastructure errors into typed application errors.

**Entrypoints**

- Parse/validate transport input.
- Resolve actor and use case.
- Serialize HTML, JSON, SSE or Telegram output.
- No business branching and no direct SQL.

**Bootstrap**

- The only layer allowed to know all concrete implementations.
- Own app lifespan, resource construction and shutdown.
- Start only workers enabled by deployment profile.

## 5. Runtime profiles

Every process starts from an explicit profile:

| Profile | Starts |
|---|---|
| `web-owner` | Web/API, owner gate, request telemetry |
| `capture-desktop` | Capture, OCR dispatch, local device health |
| `automation` | Scheduled digests, dreams, reminders, cleanup |
| `llm-worker-client` | Outbound local Ollama bridge only |
| `maintenance` | Migrations, integrity, retention, backup verification |
| `test` | No background workers unless requested by fixture |

`PERSONA_LEAN_MODE` becomes compatibility input mapped to profiles, not a large
collection of scattered conditions.

A declarative worker registry contains:

- name and owner module;
- profile and enable predicate;
- cadence/event trigger;
- CPU/IO/network resource class;
- concurrency limit;
- stop timeout;
- heartbeat/SLO;
- dependencies;
- restart policy.

## 6. Migration strategy

For each vertical slice:

1. Characterize existing behavior with API and repository contract tests.
2. Define domain types and ports.
3. Implement a SQLite/provider adapter using existing tables.
4. Implement the application use case.
5. Route old entrypoint through the new use case behind a feature flag.
6. Compare old/new outputs where safe (shadow reads, never double writes).
7. Enable for owner, observe metrics, then make default.
8. Remove old path after one rollback window.

Database schemas do not move merely to satisfy folders. Schema changes happen
only for a demonstrated invariant or performance need.

## 7. Executable backlog

### Phase P0 — stop active failure modes

#### P0.1 Owner-only production gate

- Define public allowlist: login, required static assets, explicit health probe,
  worker endpoints protected by worker token, and nothing else.
- Require authenticated owner for every other HTML/API route.
- Test anonymous, non-owner, owner, expired session and worker-token paths.
- Log denied route pattern without sensitive request content.

Exit: no non-owner can reach internal Persona UI/API; public endpoints are an
explicit reviewed list.

#### P0.2 LLM worker queue stabilization

- Empty claim performs read-before-write.
- Claim returns the row from atomic `UPDATE ... RETURNING`.
- Normal in-process flow uses Events; cross-process fallback is bounded.
- Read chunks/status with one connection.
- Chunk writes renew a job lease.
- Stale jobs become terminal errors; late workers receive HTTP 409.
- Delete terminal jobs/chunks after retention.
- Add concurrency, stale lease, cleanup and idle-write tests.

Status: first implementation landed with this plan; production observation and
load verification remain.

Exit metrics:

- zero write transactions per second for an idle queue, except maintenance at
  most once per minute;
- no duplicate job claims in concurrency test;
- stale jobs become terminal within maintenance interval;
- no unbounded terminal row growth;
- in-process chunk wake latency p95 under 100 ms.

#### P0.3 Migration ledger

- Add `schema_migration(version, name, checksum, applied_at, duration_ms)`.
- Bootstrap legacy databases in a reviewed compatibility step.
- Run each migration once in a transaction where SQLite permits.
- Verify checksum drift and fail startup loudly.
- Replace broad error suppression with migration-specific compatibility rules.
- Move optional sqlite-vec DDL into a capability-aware migration.
- Add startup lock so two processes cannot migrate concurrently.
- Test fresh install, legacy upgrade, interrupted migration, checksum mismatch
  and second-start no-op.

Exit metrics:

- second migration run executes zero migration bodies;
- no-op migration check under 250 ms on production-size schema;
- no FTS/table rebuild on ordinary startup;
- any partial/error state is visible and blocks unsafe serving.

#### P0.4 Routing and health correctness

- Fail test collection on duplicate method/path pairs.
- Select one canonical handler for each current duplicate.
- Implement cheap `/healthz` (process ready) and keep expensive diagnostics in
  `/api/health/full`.
- Add event-loop lag, DB lock wait and queue depth to internal health.

Exit: route uniqueness test passes and watchdog uses a documented readiness
endpoint with no template/DB-heavy dependency.

#### P0.5 Stabilize current feature branch

- Put unfinished AI Everywhere routes behind an owner-only feature flag.
- Register all-or-none, never partial modules.
- Generate typed/copied contracts for Copilot modes and SSE terminal event.
- Make calendar preview result the input to create; do not reparse silently.
- Complete i18n keys and contract tests.

Exit: no visible control points to an unregistered or contract-incompatible
backend.

### Phase P1 — establish dependency boundaries

#### P1.1 Architecture tests first

Add tests (AST/import-linter based) that fail on:

- new DB imports in web routes;
- framework imports in domains/application;
- cross-domain adapter imports;
- route functions over agreed complexity budget without an exemption;
- unregistered routers/workers;
- duplicate routes and settings pages absent from the settings hub.

Initial violations are stored as a shrinking baseline. New violations are
blocked immediately; baseline count must decrease in every architecture PR.

#### P1.2 Split bootstrap

- Extract router registry from `main.py`.
- Extract middleware construction.
- Extract lifespan resource manager.
- Extract profile-aware worker registry.
- Lazily import optional/heavy feature adapters.
- Keep `create_app()` as a small composition function.

Exit:

- `main.py` under 150 lines;
- one manifest lists routers and one lists workers;
- import does not create background tasks or open DB/network resources;
- cold import under 2 seconds and app construction under 1 second on reference
  hardware.

#### P1.3 Chat vertical slice

Extract in this order:

1. `ActorContext` and tenant-scoped conversation identity.
2. Load/authorize conversation use case.
3. Recall/context assembly use case.
4. Prompt policy.
5. Provider/tool orchestration state machine.
6. Message persistence unit of work.
7. SSE presenter.

The state machine has explicit states such as:

```text
accepted -> context_ready -> generating -> tool_requested
         -> tool_completed -> persisting -> completed | failed | cancelled
```

Exit:

- route handler only validates input, invokes use case and presents events;
- no SQL/FastAPI dependency in chat application/domain;
- cancellation and partial-stream persistence are explicitly tested;
- no function in the use case path exceeds 100 lines or cyclomatic complexity 12.

#### P1.4 Tenant-safe repositories

- Introduce non-interchangeable `TenantId`, `UserId`, `SystemActor`.
- Repository methods require tenant scope unless explicitly system-wide.
- Stop using numeric `0` as implicit system tenant.
- Add negative cross-tenant contract tests for every migrated repository.
- Centralize audit emission for privileged/system-wide operations.

Exit: it is impossible to call a tenant repository method without a tenant
identity in type signature.

#### P1.5 Error taxonomy

Define:

- `DomainError` for rejected valid operations;
- `NotFound` / `Forbidden` without resource enumeration leaks;
- `TransientInfrastructureError` for retryable DB/network failures;
- `ProviderError` with safe user detail;
- `InvariantViolation` for programmer/data defects.

Broad exceptions remain only at process/request boundaries, where they log,
measure and translate. Empty `except/pass` requires a documented best-effort
reason.

### Phase P2 — migrate subsystems and optimize

#### P2.1 Shared SQLite adapter

- Unit of Work owns one request/use-case connection where useful.
- Read paths never run `PRAGMA journal_mode=WAL` repeatedly.
- Central metrics: connect count, transaction duration, lock wait, rollback.
- Define write retry/backoff policy for `BUSY`, not disk I/O/corruption errors.
- Repository contract tests run against SQLite.

Targets:

- 80% reduction in connection creation on chat streaming path;
- write transaction p95 under 50 ms excluding explicitly measured bulk jobs;
- zero LLM/network calls inside write transactions.

#### P2.2 Migrate bounded contexts

Recommended sequence:

1. identity/access;
2. LLM jobs;
3. chat;
4. memory/recall/graph;
5. automation/dreams/reminders;
6. billing;
7. capture/OCR;
8. sharing/integrations;
9. remaining read-only pages.

Each context receives domain/application/adapter ownership, contract tests and a
documented public API. Do not create generic `utils`, `services` or
`repositories` dumping grounds.

#### P2.3 Background runtime

- Convert polling workers to scheduler/events where possible.
- Give CPU-heavy work bounded executors/processes.
- Add per-worker concurrency and backpressure.
- Use durable jobs for work that must survive restarts.
- Make every job idempotent or give it a dedupe key.
- Separate startup-critical tasks from deferred warmup.

Targets:

- owner web profile starts no capture/automation workers;
- idle web process performs no periodic writes except explicit heartbeat/SLO
  budget;
- graceful shutdown finishes within 10 seconds or checkpoints durable work.

#### P2.4 Frontend production pipeline

- Precompile and purge Tailwind.
- Self-host fonts or use a system stack.
- Move inline JS/CSS to versioned assets and adopt CSP.
- Add dimensions/lazy-loading for non-critical images.
- Load large libraries only on routes that use them.
- Keep HTMX/Jinja where they are effective; no framework rewrite without need.

Budgets:

- no runtime CSS compiler in production;
- landing initial compressed transfer under 150 KB excluding intentional hero
  media;
- no third-party request on owner/login/landing unless documented and opted in;
- CLS under 0.1, LCP under 2.5 s on defined mobile profile.

#### P2.5 API consolidation

- Classify commands, queries, HTML pages, feeds and internal callbacks.
- Merge duplicate narrow endpoints behind coherent resources/use cases.
- Version externally consumed JSON APIs.
- Publish deprecation windows and compatibility tests.
- Reduce route count based on product semantics, not an arbitrary quota.

### Phase P3 — enforce excellence

#### P3.1 Quality gates

- Ruff high-signal rules pass with Russian typography checks configured
  intentionally.
- Mypy strict for domains/application and progressively for adapters.
- Full test suite deterministic and under an agreed CI budget.
- Mutation tests for critical policies.
- Dependency and secret scanning.

#### P3.2 Reliability drills

- Kill worker during stream and verify terminal recovery.
- Kill process during migration and verify safe restart.
- Simulate SQLite BUSY, disk full and read-only filesystem.
- Restore encrypted backup into a clean environment.
- Load-test chat, search and capture ingestion together.

#### P3.3 Documentation as a release artifact

- Generate route/worker inventory.
- Keep one authoritative runbook per runtime profile.
- Add architecture decision records for dependency exceptions.
- Validate README version, health routes and commands in CI.

## 8. Definition of “10/10”

“10/10” is not zero defects. It means the architecture makes defects local,
observable and inexpensive to fix. Persona reaches the target only when all
conditions below are continuously enforced.

### Boundaries

- Zero direct DB imports in entrypoint routes.
- Zero FastAPI/Jinja/aiosqlite imports in domain/application.
- All cross-boundary dependencies expressed as typed ports.
- Architecture tests block regressions.

### Correctness and security

- Owner/tenant authorization is centralized and negative-tested.
- Every write use case defines transaction and idempotency behavior.
- Duplicate routes: zero.
- Critical queues recover from crash/retry without duplicate effects.
- Migration checksum and applied state are verified.

### Performance

- Cold web import under 2 s; app construction under 1 s.
- Readiness endpoint p99 under 50 ms locally and independent of templates/LLM.
- Idle web profile has zero repeated write-lock polling.
- Chat transport overhead p95 under 100 ms excluding model generation.
- Defined page Core Web Vitals meet “good” thresholds.

### Maintainability

- Route/transport handlers normally under 50 lines.
- Application functions normally under 100 lines and complexity 12.
- Exceptions require a nearby architectural decision/exemption test.
- New feature work includes use-case, adapter and entrypoint tests.
- No cyclic package dependencies.

### Reliability

- Request, job and worker metrics have SLO dashboards/alerts.
- Graceful shutdown and restart recovery are tested.
- Backup restoration is automatically verified.
- Full suite and production smoke tests pass before deploy.

### Product consistency

- Runtime, service worker, package and API metadata versions agree.
- Documentation commands and health endpoints are executable in CI.
- Privacy claims match actual network requests.

## 9. Test pyramid and required suites

| Suite | Scope | Runtime target |
|---|---|---:|
| Domain unit | Policies/invariants, no I/O | under 10 s |
| Application unit | Use cases with fake ports | under 30 s |
| Adapter contract | SQLite/providers against real adapter | under 90 s |
| Entrypoint contract | Auth, validation, JSON/SSE/HTML | under 90 s |
| Architecture | Imports, routes, registries, complexity baseline | under 10 s |
| Integration | Selected complete vertical slices | under 3 min |
| E2E smoke | Owner login, chat, search, worker recovery | under 5 min |
| Load/recovery | Scheduled/nightly | reported trend |

Coverage is a supporting metric:

- domain/application critical contexts: at least 90% branch coverage;
- security, billing, migrations, queue lifecycle: 100% invariant-path coverage;
- whole repository: no decreasing coverage without justification.

## 10. Metrics to record before every phase

- process import/start/readiness duration;
- event-loop lag p50/p95/p99;
- SQLite connects/sec and writes/sec while idle/active;
- write lock wait and transaction duration;
- job queue depth, age, lease expiry and terminal cleanup;
- request duration/error rate by route/use case;
- background worker count by profile;
- JS/CSS/image transfer per main page;
- architecture violation baseline;
- full suite duration and flake rate.

Store reference commands and results in CI artifacts, not manually edited claims.

## 11. Release and rollback rules

- One bounded-context migration per change set.
- Schema migrations are additive until rollback window expires.
- New path sits behind an owner-only feature flag when behavior is risky.
- Shadow reads are permitted; shadow writes are forbidden.
- Every rollout has old-path fallback or a documented irreversible migration.
- Production observation window is required before deleting compatibility code.
- Internal refactors do not bump product/service-worker versions unless they
  change shipped browser behavior or release policy explicitly requires it.

## 12. Immediate next sequence

1. Deploy and observe P0.2 queue stabilization.
2. Implement P0.3 migration ledger before adding migration 204+.
3. Add route uniqueness and `/healthz` tests.
4. Stabilize/flag AI Everywhere.
5. Add architecture-test baseline.
6. Split bootstrap and runtime profiles.
7. Migrate chat as the first major vertical slice.
8. Continue bounded contexts in the P2 order.

This order attacks availability first, then prevents new structural debt, then
extracts the highest-complexity behavior with tests and rollback at every step.
