# Persona LLM Worker on Windows

The local LLM worker runs on the PC with Ollama and makes outbound requests to
the Persona server. No inbound tunnel is required.

## Owner-PC bootstrap (no repository required)

Open `/settings/automation` as the primary owner, create a five-minute
enrollment ticket and copy its generated one-command bootstrap to the Windows
PC. The bootstrap exchanges the ticket once over HTTPS, receives independent
LLM and browser credentials, validates both, and stores them in a local `.env`
whose ACL grants access only to the current Windows user.

The permanent credentials are not displayed in the UI or passed on the command
line. See [WORKER_ENROLLMENT.md](WORKER_ENROLLMENT.md) for the trust model,
secure-prompt alternative and recovery procedure.

## One-command provisioning, autostart and first run

For repository-based maintenance and recovery, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -ProvisionToken -StartNow
```

`-ProvisionToken` is deliberately explicit because it invalidates the previous
LLM and browser worker tokens. A repository Python helper rotates two independent
server-side credentials and atomically updates `.env`; it never writes either
token to console or command-line arguments. Existing `.env` content is preserved
and its previous state is backed up to `.env.persona-worker.bak`.

If the token is already configured, omit token rotation:

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -StartNow
```

The per-user `PersonaLLMWorker` task:

- starts at user logon and uses `StartWhenAvailable`;
- runs hidden without administrator privileges;
- ignores duplicate starts;
- restarts after unexpected process failure;
- starts a local `ollama serve` automatically when Ollama was closed;
- launches `ops/persona_llm_worker.ps1`, whose own supervisor reconnects and
  restarts the Python worker.

Automatic Ollama startup applies only to loopback `OLLAMA_URL` values
(`127.0.0.1`, `localhost` or `::1`). A remote Ollama endpoint is never managed
by the worker.

It does not start the Persona web server or Telegram worker. Those have separate
lifecycles.

## Diagnose

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -Status
```

This reports task state, last result, whether a durable token is configured and
whether local Ollama and the Persona server are reachable. It never prints the
token. A failed server check is a warning rather than an installation blocker:
the worker keeps reconnecting with backoff.

Useful native inspection:

```powershell
Get-ScheduledTask -TaskName PersonaLLMWorker
Get-ScheduledTaskInfo -TaskName PersonaLLMWorker
```

## HTTPS proxy

If `persona.getdoday.ru` is not reachable directly from the worker PC, use the
standard proxy variables. They may be stored in `.env` for the Scheduled Task:

```dotenv
HTTPS_PROXY=http://127.0.0.1:8080
HTTP_PROXY=http://127.0.0.1:8080
NO_PROXY=127.0.0.1,localhost
```

SOCKS proxies can be configured through `ALL_PROXY` when the installed httpx
transport supports that proxy type. Never put the worker token in a proxy URL.

The outbound Persona HTTP client explicitly honors the environment proxy
configuration. Local Ollama HTTP calls explicitly bypass it, so model traffic
does not accidentally leave the PC.

## Remove

```powershell
powershell -ExecutionPolicy Bypass -File .\ops\install_llm_worker_windows.ps1 -Uninstall
```

Removing the task does not delete `.env`, Ollama models or Persona data.
