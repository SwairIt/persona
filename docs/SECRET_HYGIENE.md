# Secret hygiene

Persona is meant to go public. A credential that reaches a commit is public
the moment the repo is — and **stays public even if you delete it in a later
commit**, because git keeps the old blob. This page describes the three layers
that keep that from happening, and what to do when one of them fires.

## TL;DR

```sh
sh ops/install_hooks.sh          # once per clone — installs the pre-commit hook
python ops/secret_scan.py --tree # audit the whole tracked tree at any time
```

Secrets live in **`.env`** (ignored) or under **`PERSONA_DATA_DIR`**
(`~/.persona/`, outside the repo entirely). Never in code, never in the DB as
plaintext, never in a commit.

---

## The three layers

| Layer | File | Catches | Runs |
|---|---|---|---|
| 1. `.gitignore` | `.gitignore` | whole classes of files (`.env*`, `*.db`, `*.pem`, logs, scratch) | always |
| 2. pre-commit hook | `ops/hooks/pre-commit` | credential-shaped strings in the **staged diff**, plus logging leaks in staged `.py` | on `git commit`, if installed |
| 3. pytest | `tests/test_no_secrets_committed.py` | the same, over the **whole tracked tree**, plus `.env.example` completeness | on every test run / CI |

Layers 2 and 3 share one rule set — `ops/secret_scan.py` — so they cannot
drift apart. Layer 2 is fast but opt-in per clone; layer 3 is slower but
protects everyone, including a commit made with `--no-verify`.

## Installing the hook

```sh
sh ops/install_hooks.sh
```

or, from a plain PowerShell prompt:

```powershell
powershell -ExecutionPolicy Bypass -File ops\install_hooks.ps1
```

Either installs a shim at `.git/hooks/pre-commit` that delegates to the
versioned `ops/hooks/pre-commit`. Because it is a shim, edits to the real hook
take effect immediately — no reinstall. Git for Windows runs hooks through its
bundled POSIX `sh`, so the same hook works from Git Bash, PowerShell, cmd, and
GUI clients.

`.git/hooks/` is not versioned, so **every clone needs this run once.** That is
exactly why layer 3 exists.

Uninstall with `rm .git/hooks/pre-commit`.

### Coexisting with the `pre-commit` framework

This repo also has a `.pre-commit-config.yaml` (ruff/mypy/pytest). If you run
`pre-commit install`, it renames our hook to `pre-commit.legacy` and still
chains to it, so both keep working. The installer likewise backs up any
pre-existing hook before writing.

## What gets flagged

`ops/secret_scan.py` matches credential **shapes at real-world lengths**, not
the word "token". A scanner that cries wolf gets bypassed, and then it protects
nothing. Currently detected:

- Provider keys: OpenAI (`sk-`, `sk-proj-`), Anthropic (`sk-ant-apiNN-`),
  OpenRouter (`sk-or-v1-`), Groq (`gsk_`), Google (`AIza…`), AWS (`AKIA…`),
  Stripe, Brave
- GitHub PATs — classic (`ghp_` + 36) and fine-grained (`github_pat_…`)
- Slack tokens and incoming-webhook URLs
- Telegram bot tokens (`<id>:AA…`) and Telethon `StringSession` literals
- YooKassa secret keys (`live_…` / `test_…`)
- JWTs, PEM/PuTTY private-key blocks
- Credentials embedded in a URL (`scheme://user:password@host`)
- A real value assigned to a known secret env var (`PERSONA_WORKER_TOKEN=`,
  `PERSONA_SMTP_PASS=`, `PERSONA_YOOKASSA_SECRET_KEY=`, `PERSONA_TG_USER_API_HASH=`, …)
- Filenames that must never be tracked: `.env*` (except `.env.example`),
  `billing_secrets.json`, `*.pem/.pfx/.p12/.ppk`, `id_rsa`, `*.db/.sqlite*`,
  `*.session`, `.netrc`, `.npmrc`, `.pypirc`

Length thresholds are deliberate: `ghp_` + 36 chars is a live PAT, `ghp_` + 32
is somebody's fixture. Only the former is flagged.

### Credentials written to the log

Separately, the scanner parses staged `.py` files and flags **logging calls
that pass a credential straight through** — the bug class fixed in `6a806dc`,
where magic-link tokens ended up in plaintext server logs:

```python
logger.error("share_corrupt", token=token)        # flagged
logger.error("share_corrupt", token_fp=fp(token)) # fine
```

This uses the AST, not a regex, because a regex over log lines is hopelessly
noisy here — `reason="missing_token"`, `token_id=…` and `max_tokens=…` all
match textually. The rule only fires when a keyword argument or f-string slot
*is* the secret: a bare name or attribute called `token`, `secret`,
`password`, `api_key`, `device_token`, … Measured across this codebase it
produces **2 hits and 0 false positives**.

It matches locally-named loggers too (`cover_log`, `audit_logger`), not just
`log`/`logger`. That detail is load-bearing: the first version only matched
the bare names and silently missed two real leaks behind a `cover_log`
binding.

**Baseline.** `KNOWN_LOGGING_LEAKS` in `ops/secret_scan.py` lists occurrences
that already exist and are owned by someone else, so the rule can be enforced
against new code without blocking on someone else's fix. Entries are keyed by
`<path>:<log event name>` (stable when lines move) and each needs a reason.
Two tests keep the baseline honest: new leaks fail, **and** an entry whose
leak has since been fixed fails as stale so it gets deleted — otherwise a
stale entry keeps suppressing its key forever, which is exactly how the first
version of this baseline hid two live leaks.

### `.env.example` completeness

`test_env_example_documents_every_secret_variable` scans `app/`, `ops/`,
`scripts/` and `mac-agent/` for secret-shaped env var names and fails if one
is not documented in `.env.example`. An undocumented secret variable is how
secrets end up hardcoded: the next person cannot find where the value belongs,
so they inline it. A companion test asserts every secret-shaped key in
`.env.example` is **empty** — it is a template, not a config.

Names that match the shape but are not secrets (`PERSONA_SESSION_IDLE_DAYS` is
a lifetime in days, not a session token) live in `_NOT_ACTUALLY_SECRET` in the
test, each with a reason.

## When the scanner fires

### It is a real secret

1. **Do not just amend the file.** Unstage it: `git restore --staged <file>`.
   If it is already committed locally but **not pushed**, rewrite before
   pushing (`git reset --soft`, or `git rebase -i`).
   If it is already **pushed**, deleting it in a new commit does *not* help —
   the blob stays reachable.
2. Move the value into `.env` or `{PERSONA_DATA_DIR}/billing_secrets.json`.
3. **Rotate it.** Assume anything that reached a commit — even locally, even
   for a second — is compromised. Rotation is cheap; a leaked YooKassa secret
   or Telegram session is not.

### It is a false positive

Pick the narrowest fix:

1. **Inline (preferred)** — append a marker comment to that line:

   ```python
   SAMPLE = "AKIAIOSFODNN7EXAMPLE"  # secret-scan: ignore
   ```

   Narrow, reviewable in a diff, and expires with the line.

2. **Whole file** — add it to `EXCLUDED_PATHS` in `ops/secret_scan.py`
   **with a reason**. `tests/test_no_secrets_committed.py` asserts that every
   entry has a reason *and* that the file still exists, so stale exclusions
   cannot pile up and hide the next leak.

3. **One-off bypass** — for an emergency only:

   ```sh
   PERSONA_SKIP_SECRET_SCAN=1 git commit ...
   ```

   `git commit --no-verify` also skips it. Both leave layer 3 (pytest) armed,
   which is the point.

Currently excluded files are all detection-pattern or fixture files — the PII
redaction packs, the outbound-projection guards, and the tests that assert
those redactors work. Each is annotated in `ops/secret_scan.py`.

## Where secrets are supposed to live

| Secret | Home |
|---|---|
| LLM/API keys (per user) | `kv_setting` in the DB (`byo_api_key_*`), or `PERSONA_BYO_API_KEY` |
| SMTP user/password | `.env` → `PERSONA_SMTP_USER` / `PERSONA_SMTP_PASS` |
| YooKassa `shop_id` + `secret_key` | env, else `{PERSONA_DATA_DIR}/billing_secrets.json` (mode 0600) |
| Telegram `api_id`/`api_hash` | `.env` → `PERSONA_TG_USER_API_*` |
| Telegram user session | `{PERSONA_DATA_DIR}/` — never the repo |
| Worker tokens | `.env` / machine env → `PERSONA_WORKER_TOKEN`, `PERSONA_BROWSER_WORKER_TOKEN` |

`.env.example` documents every variable with an **empty** value. Keep it that
way — it is a template, not a config.

## Other places a secret can still escape

The scanner only guards **git**. These surfaces move real data off the box and
need their own care:

- **`/api/export/full.zip`** (`app/web/routes/full_export.py`) ships the raw
  `persona.db`, which includes the `kv_setting` table — i.e. plaintext API
  keys. Owner-authenticated, but the zip itself is unredacted. Treat that file
  like a password vault; never attach it to a bug report.
- **Diagnostics bundle** (`app/diagnostics_bundle.py`) *is* redacted — it
  blanks any `kv_setting` key matching `api_key`/`secret`/`password`/`token`/
  `private_key`/`access_token`/`refresh_token`. Prefer it over `full.zip`
  when sharing state for support.
- **Encrypted backups** (`app/backup/`, `scripts/encrypted_backup.py`) contain
  the same DB. Keep the master password out of the repo.
- **Crash dumps / uvicorn logs** (`*.log`, ignored) can contain request bodies.
  Do not paste them wholesale into issues.
