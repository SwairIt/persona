# Owner PC worker enrollment

Persona enrolls the owner's outbound Windows worker without asking the owner to
copy either permanent worker credential.

## Owner onboarding

1. Sign in as the **primary owner** and open `/settings/automation`.
2. Under **Owner PC worker**, optionally bind the ticket to a known stable
   worker ID, then create a ticket.
3. Copy the generated PowerShell command to the owner PC and run it within five
   minutes.

The command downloads the public bootstrap and passes a short-lived ticket:

```powershell
& ([scriptblock]::Create((irm https://persona.getdoday.ru/api/llm/worker/bootstrap.ps1))) -EnrollmentTicket '<one-use-ticket>'
```

The ticket is not placed in a URL, HTTP header or server log. It is present
briefly in PowerShell history and process arguments, so run the command only on
the trusted owner PC. Its five-minute lifetime and one-use consume limit that
exposure. To avoid shell history entirely, run the public command below and
paste the ticket into its hidden secure prompt:

```powershell
irm https://persona.getdoday.ru/api/llm/worker/bootstrap.ps1 | iex
```

## Security contract

- Only an authenticated **primary owner** can issue a ticket. Full-access
  delegates cannot issue one.
- The plaintext ticket is returned once with `Cache-Control: no-store`.
  SQLite stores only its SHA-256 digest.
- A ticket expires after five minutes and can be consumed once. Issuing a new
  ticket revokes the previous unused ticket or pending, unactivated enrollment.
- A ticket is always scoped to the combined enrollment capability
  `llm+browser`; it may additionally be bound to an expected stable worker ID.
- Exchange accepts the ticket only in a small JSON body over HTTPS (loopback
  HTTP is allowed for local development). It does not accept an LLM token as
  proof of enrollment.
- Exchange consumes the ticket and stores only paired pending credential
  hashes; it does not rotate either active worker credential. Concurrent
  exchange has exactly one winner.
- Pending activation has its own 24-hour deadline so Python, Playwright,
  Chromium, Ollama and models can finish installing after the five-minute
  ticket exchange window closes.
- Activation proves possession of both pending plaintext credentials and the
  bound worker ID, then rotates both active hashes and records `activated_at`
  in one transaction. Retrying an already successful activation is idempotent,
  including recovery after a lost response.
- The LLM and browser credentials are independently random and scoped. Neither
  credential authorizes the other queue, and either can still be rotated by the
  existing recovery tooling.
- Successful issuance/exchange and known replay, expiry, revocation or binding
  failures are audited using only ledger IDs, worker IDs and result metadata.
  Tickets and returned credentials are never audited.
- Deleting the owner cascades the enrollment ledger, so account erasure is not
  blocked by stale tickets.

## Failure recovery and health

Immediately after exchange, the bootstrap atomically writes both credentials
to an owner-only `.env.next`, leaving the active `.env`, worker scripts and
tasks untouched. Heavy downloads and model installation happen next. A failed
run resumes from `.env.next` without a new ticket. Only after runtime preflight
does the bootstrap promote staged files, activate credentials, probe both
capabilities, replace both Scheduled Tasks as one rollback unit, and wait for
heartbeat files written after real authenticated polls.

If an activation response is lost, rerun the same bootstrap command without a
new ticket. The promoted `.env` retains enrollment metadata and the server
returns the original activation result idempotently. Never paste permanent
tokens into chat, logs or command arguments.

The server cannot recover plaintext credentials if the exchange HTTP response
itself is lost: it stores hashes only. Create a new ticket in that case; issue
atomically revokes the unreachable pending enrollment. This does not rotate the
still-active credentials.

After activation, the previous server credentials are intentionally no longer
valid, so task rollback restores definitions but cannot restore the old
credential state. The promoted `.env` and enrollment metadata remain durable;
rerunning the bootstrap repeats probes and the idempotent activation, then
retries the two-task switch.

Server operators can correlate enrollment health through the safe audit
actions `worker.enrollment.issue`, `worker.enrollment.exchange`, and
`worker.enrollment.activate`; ticket and credential plaintext never appears
there.

Existing installations that already provide both
`PERSONA_WORKER_TOKEN` and `PERSONA_BROWSER_WORKER_TOKEN` remain supported and
do not need enrollment on every bootstrap run.
