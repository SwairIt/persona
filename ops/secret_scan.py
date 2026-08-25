#!/usr/bin/env python3
"""High-signal secret scanner shared by the pre-commit hook and pytest.

Two entry points, one rule set — so a commit that the hook would block is
also a red pytest, and vice versa:

    python ops/secret_scan.py --staged   # only the lines a commit would add
    python ops/secret_scan.py --tree     # every tracked file at HEAD

Design rules (why this file is boring on purpose):

* **High signal only.** Every pattern matches a credential *shape* that a
  real provider issues, with the real length. Not the word ``token``.
  A scanner that cries wolf gets ``--no-verify``-ed into irrelevance, and
  then it protects nothing.
* **Length is the filter.** ``ghp_`` + 36 chars is a GitHub PAT;
  ``ghp_`` + 32 is somebody's test fixture. We match the former only.
* **Escape hatches are documented, not secret.** See ``docs/SECRET_HYGIENE.md``.

Exit code 0 = clean, 1 = findings, 2 = usage error.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

# Each entry: (label, compiled regex). Keep the regexes anchored on a vendor
# prefix + a real-world length so that placeholders and fixtures do not match.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # --- generic provider keys -------------------------------------------
    ("OpenAI key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("OpenAI project key", re.compile(r"\bsk-proj-[A-Za-z0-9_-]{60,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-api\d{2}-[A-Za-z0-9_-]{80,}\b")),
    ("OpenRouter key", re.compile(r"\bsk-or-v1-[a-f0-9]{48,}\b")),
    ("Groq key", re.compile(r"\bgsk_[A-Za-z0-9]{45,}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("AWS access key id", re.compile(r"\bAKIA[A-Z0-9]{16}\b")),
    ("GitHub PAT (classic)", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GitHub PAT (fine-grained)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("GitHub OAuth/app token", re.compile(r"\bgh[osur]_[A-Za-z0-9]{36}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9]{10,}-[0-9]{10,}-[A-Za-z0-9]{20,}\b")),
    (
        "Slack webhook URL",
        re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]{7,}/B[A-Z0-9]{7,}/[A-Za-z0-9]{20,}"),
    ),
    ("Stripe secret key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{24,}\b")),
    ("Brave / generic 32-hex key header", re.compile(r"BSA[A-Za-z0-9_-]{28,}\b")),
    # --- Telegram ---------------------------------------------------------
    # Bot tokens are "<numeric id>:AA<35 chars>" — a very distinctive shape.
    ("Telegram bot token", re.compile(r"\b\d{8,11}:AA[A-Za-z0-9_-]{33}\b")),
    # Telethon/Pyrogram StringSession blobs are enormous base64 strings.
    (
        "Telethon StringSession literal",
        re.compile(r"StringSession\(\s*[\"'][A-Za-z0-9+/=_-]{100,}"),
    ),
    # --- YooKassa ---------------------------------------------------------
    # live_/test_ + base64url. The lookaheads demand mixed case AND a digit so
    # that long snake_case identifiers (`test_dedup_collapses_near_duplicates`)
    # do not match — that was a real false positive on this repo's test suite.
    (
        "YooKassa secret key",
        re.compile(
            r"\b(?:live|test)_(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{40,}\b"
        ),
    ),
    # --- JWTs -------------------------------------------------------------
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}")),
    # --- private keys -----------------------------------------------------
    (
        "PEM private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    ("PuTTY private key", re.compile(r"PuTTY-User-Key-File-\d")),
]

# Credentials embedded in a URL: scheme://user:password@host
_CRED_URL = re.compile(
    r"\b[a-z][a-z0-9+.-]*://"  # scheme
    r"(?P<user>[^/@:\s\"'<>{}$]{1,64})"  # user
    r":(?P<pw>[^/@\s\"'<>{}$]{6,128})"  # password
    r"@(?P<host>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)

# Hosts / passwords that make a credential URL obviously illustrative.
_CRED_URL_SAFE_HOSTS = re.compile(
    r"^(localhost|127\.0\.0\.1|\[?::1\]?|.*\.invalid|.*\.example|"
    r"example\.(com|org|net)|.*\.example\.(com|org|net)|.*\.test|.*\.local)$",
    re.IGNORECASE,
)
_CRED_URL_SAFE_WORDS = re.compile(
    r"^(pass|passwd|password|secret|token|hunter2|changeme|xxx+|placeholder|"
    r"your[-_]?password|\*+|\.\.\.)$",
    re.IGNORECASE,
)

# `KEY=value` assignments for secrets this project actually uses. A real value
# is flagged; empty / commented / obviously-placeholder values are not.
# Kept in sync with:
#   git grep -ohE 'PERSONA_[A-Z0-9_]*(TOKEN|SECRET|KEY|PASS|PASSWORD|HASH)[A-Z0-9_]*' \
#       -- 'app/**' 'ops/**' 'scripts/**' 'mac-agent/**' | sort -u
_SECRET_ENV_NAMES = (
    # workers
    "PERSONA_WORKER_TOKEN",
    "PERSONA_BROWSER_WORKER_TOKEN",
    "PERSONA_AGENT_TOKEN",
    # mail
    "PERSONA_SMTP_PASS",
    "PERSONA_SMTP_PASSWORD",
    # billing
    "PERSONA_YOOKASSA_SECRET_KEY",
    "PERSONA_YOOKASSA_SHOP_ID",
    "PERSONA_ROBOKASSA_PASSWORD1",
    "PERSONA_ROBOKASSA_PASSWORD2",
    "PERSONA_ROBOKASSA_TEST_PASSWORD1",
    "PERSONA_ROBOKASSA_TEST_PASSWORD2",
    "PERSONA_ROBOKASSA_HASH",
    # telegram
    "PERSONA_TG_USER_API_HASH",
    "PERSONA_TG_SESSION",
    "PERSONA_TG_BOT_TOKEN",
    "PERSONA_TG_PAIRING_SECRET",
    # crypto at rest
    "PERSONA_VAULT_MASTER_PASSWORD",
    "PERSONA_BACKUP_PASSPHRASE",
    "PERSONA_BACKUP_PASSWORD",
    # LLM / search providers
    "PERSONA_BYO_API_KEY",
    "PERSONA_BRAVE_API_KEY",
    "BRAVE_API_KEY",
    "BRAVE_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "XAI_API_KEY",
    "TOGETHER_API_KEY",
    # cloud / vcs
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
_SECRET_ENV_ASSIGN = re.compile(
    r"^\s*(?:export\s+|set\s+|\$env:)?(" + "|".join(_SECRET_ENV_NAMES) + r")\s*=\s*(?P<val>\S.*)$"
)
_ENV_PLACEHOLDER = re.compile(
    r"^("
    r"|\$.*|%.*%|<.*>|\{.*\}|x{3,}|\*+"  # ${VAR}, %VAR%, <put-key-here>, xxx
    r"|.*(\.\.\.|…)"  # anything elided: sk-ant-..., ghp_…
    r"|your[-_].*|changeme|placeholder|none|null|todo|test|dummy|fake|example"
    r")$",
    re.IGNORECASE,
)

# A value that reads as an English phrase is documentation, not a credential.
# The only real secrets containing spaces are Gmail-style app passwords, which
# are fixed groups of exactly four characters — so a word of five or more
# letters means prose ("at least twelve chars"), not a password.
_ENV_PROSE = re.compile(r"^(?=.*\s)(?=.*[A-Za-z]{5,})[A-Za-z ]+$")


def _normalize_env_value(raw: str) -> str:
    """Strip comments, shell line-continuations and matching quotes."""
    val = raw.split("#", 1)[0].strip()
    val = re.sub(r"\\+$", "", val).strip()  # trailing ` \` continuation
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1].strip()
    return val


# Filenames that must never be committed, whatever their content.
_FORBIDDEN_NAMES = re.compile(
    r"(^|/)("
    r"\.env(\.[A-Za-z0-9_.-]+)?"  # .env, .env.local, .env.prod.bak
    r"|billing_secrets\.json"
    r"|id_rsa|id_dsa|id_ecdsa|id_ed25519"
    r"|.*\.(pem|pfx|p12|jks|keystore|ppk)"
    r"|.*\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?"
    r"|.*\.session(-journal)?"
    r"|credentials\.json|service[-_]account.*\.json"
    r"|\.npmrc|\.pypirc|\.netrc"
    r")$",
    re.IGNORECASE,
)
# ...except these, which are legitimately templates or app source.
_FORBIDDEN_NAME_ALLOW = re.compile(
    r"(^|/)("
    r"\.env\.(example|sample|template|dist)"
    r"|app/adapters/remote_browser/credentials\.py"
    r")$",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Path exclusions — files that legitimately contain secret-*shaped* strings.
# Each entry needs a reason; "it was noisy" is not a reason.
# --------------------------------------------------------------------------
EXCLUDED_PATHS: dict[str, str] = {
    # The PII/secret redaction engine ships example matches for its own
    # detection rules, rendered in the UI so the owner can see what a rule
    # catches. Detection patterns, not credentials.
    "app/redaction_packs.py": "documents its own detection rules with sample matches",
    # This scanner defines the patterns it hunts for.
    "ops/secret_scan.py": "defines the detection patterns themselves",
    "tests/test_no_secrets_committed.py": "asserts on the detection patterns",
    "docs/SECRET_HYGIENE.md": "documents the detection patterns",
    # Outbound-projection guards: regexes that stop Persona from *sending* a
    # secret to Telegram. Same reason as redaction_packs.
    "app/domains/projection/policy.py": "redaction regexes for outbound text",
    "app/domains/autowake/policy.py": "redaction regexes for outbound text",
    # Fixtures asserting that redaction/projection actually redacts. The
    # strings are AWS's published documentation example and the jwt.io demo
    # token — public, non-functional, and the test is meaningless without them.
    "tests/test_projection_outbox.py": "fixtures: AWS doc example key + jwt.io demo JWT",
    "tests/test_redaction.py": "fixtures: fabricated keys the redactor must catch",
    "tests/test_llm_sharing.py": "fixtures: fabricated per-user API keys",
    "tests/test_copilot_member.py": "fixture: fabricated Groq key",
}

# Vendored / generated content we never author.
EXCLUDED_DIR_PARTS = (
    "app/web/static/vendor/",
    "node_modules/",
    ".venv/",
    "site-packages/",
)

# An inline escape hatch for a one-off false positive on a single line.
INLINE_ALLOW = re.compile(r"secret-scan:\s*(ignore|allow)", re.IGNORECASE)

MAX_FILE_BYTES = 1_000_000


# --------------------------------------------------------------------------
# Logging leaks (Python AST)
#
# The bug class fixed in 6a806dc: a logging call that interpolates a live
# credential, putting it in plaintext in the server log. A regex over log
# lines is far too noisy here (`reason="missing_token"`, `token_id=…`,
# `max_tokens=…` all match), so this uses the AST and only flags a kwarg or
# f-string slot whose VALUE is a bare name/attribute that IS the secret —
# `token=token`, not `token_id=token_id`. Measured across this codebase that
# is 1 hit and 0 false positives.
# --------------------------------------------------------------------------

# Matches `log` / `logger` / `logging` and locally-named loggers such as
# `cover_log` or `audit_logger`. Narrowing this to the bare names missed two
# real leaks behind a `cover_log` binding, so the suffix form is load-bearing.
_LOG_OBJECT = re.compile(r"^(_?(log|logger|logging)|[A-Za-z0-9_]+_(log|logger))$", re.IGNORECASE)
_LOG_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})
_SECRET_VALUE_NAME = re.compile(
    r"^(token|secret|password|passwd|passphrase|api_key|apikey|access_token"
    r"|refresh_token|bot_token|session_string|private_key|master_password"
    r"|secret_key|device_token|agent_token|worker_token)$",
    re.IGNORECASE,
)

# Occurrences that already exist in the tree, kept as an explicit baseline so
# the rule can be enforced against NEW code without blocking on a fix that
# belongs to another owner. Key: "<path>:<log event name>" — stable when lines
# move. Removing a fixed entry is safe; the check is a subset test.
# Empty on purpose: both original entries (share_collection_cover_set /
# share_collection_cover_missing) were fixed on 2026-08-25 — they now log
# ``token_fp=`` from ``share_collection.token_fingerprint`` instead of the raw
# capability token — and a fixed entry MUST be deleted, or it keeps suppressing
# its key forever (see test_logging_leak_baseline_has_no_stale_entries).
KNOWN_LOGGING_LEAKS: dict[str, str] = {}


def _log_event_name(node: ast.Call) -> str:
    """First positional string arg — structlog's event name."""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return f"line{node.lineno}"


def _value_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_log_call(node: ast.AST) -> bool:
    """True for `log.info(...)`, `cover_log.error(...)`, `audit_logger.warning(...)`."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if not (isinstance(fn, ast.Attribute) and fn.attr in _LOG_LEVELS):
        return False
    return isinstance(fn.value, ast.Name) and bool(_LOG_OBJECT.match(fn.value.id))


def _leaked_arguments(node: ast.Call) -> list[str]:
    """Names of credential values this logging call would write out verbatim."""
    leaked: list[str] = []
    for kw in node.keywords:
        name = _value_name(kw.value)
        if name and _SECRET_VALUE_NAME.match(name):
            leaked.append(f"{kw.arg}={name}")
    for arg in [*node.args, *(k.value for k in node.keywords)]:
        if not isinstance(arg, ast.JoinedStr):
            continue
        for part in arg.values:
            if not isinstance(part, ast.FormattedValue):
                continue
            name = _value_name(part.value)
            if name and _SECRET_VALUE_NAME.match(name):
                leaked.append(f"f-string {{{name}}}")
    return leaked


def scan_python_logging(path: str, text: str, *, apply_baseline: bool = True) -> list[Finding]:
    """Flag logging calls that pass a credential value straight through.

    ``apply_baseline=False`` reports every occurrence including baselined ones,
    which is how the test detects a baseline entry that has since been fixed.
    """
    if is_excluded(path) or not path.endswith(".py"):
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []

    norm = path.replace("\\", "/").lstrip("./")
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not _is_log_call(node):
            continue
        assert isinstance(node, ast.Call)  # narrowed by _is_log_call

        baseline_key = f"{norm}:{_log_event_name(node)}"
        if apply_baseline and baseline_key in KNOWN_LOGGING_LEAKS:
            continue

        for what in _leaked_arguments(node):
            out.append(
                Finding(
                    norm,
                    node.lineno,
                    f"logging call leaks a credential ({what})",
                    f"[{baseline_key}] log a fingerprint/hash instead of the value",
                )
            )
    return out


@dataclass(frozen=True)
class Finding:
    path: str
    line_no: int
    label: str
    excerpt: str

    def render(self) -> str:
        return f"  {self.path}:{self.line_no}: {self.label}\n      {self.excerpt}"


# --------------------------------------------------------------------------
# Core matching
# --------------------------------------------------------------------------


def _mask(text: str) -> str:
    """Never echo a live credential into a terminal or CI log.

    Only long unbroken token-ish runs are masked; ordinary identifiers stay
    readable, otherwise the report is useless for locating the line.
    """
    text = text.strip()
    if len(text) > 160:
        text = text[:160] + "..."
    return re.sub(
        r"[A-Za-z0-9+/=_-]{24,}",
        lambda m: m.group(0)[:6] + "...<redacted:" + str(len(m.group(0))) + "chars>",
        text,
    )


def is_excluded(path: str) -> bool:
    norm = path.replace("\\", "/").lstrip("./")
    if norm in EXCLUDED_PATHS:
        return True
    return any(part in norm for part in EXCLUDED_DIR_PARTS)


def scan_line(path: str, line_no: int, line: str) -> list[Finding]:
    if INLINE_ALLOW.search(line):
        return []
    out: list[Finding] = []
    for label, rx in PATTERNS:
        if rx.search(line):
            out.append(Finding(path, line_no, label, _mask(line)))
    for m in _CRED_URL.finditer(line):
        host, pw = m.group("host"), m.group("pw")
        if _CRED_URL_SAFE_HOSTS.match(host) or _CRED_URL_SAFE_WORDS.match(pw):
            continue
        out.append(Finding(path, line_no, "credentials embedded in URL", _mask(line)))
    env_m = _SECRET_ENV_ASSIGN.match(line)
    if env_m:
        val = _normalize_env_value(env_m.group("val"))
        if val and not _ENV_PLACEHOLDER.match(val) and not _ENV_PROSE.match(val):
            label = f"{env_m.group(1)} assigned a real value"
            out.append(Finding(path, line_no, label, _mask(line)))
    return out


def scan_text(path: str, text: str) -> list[Finding]:
    if is_excluded(path):
        return []
    out: list[Finding] = []
    for i, line in enumerate(text.splitlines(), start=1):
        out.extend(scan_line(path, i, line))
    return out


def check_filename(path: str) -> list[Finding]:
    norm = path.replace("\\", "/").lstrip("./")
    if _FORBIDDEN_NAME_ALLOW.search(norm):
        return []
    if _FORBIDDEN_NAMES.search(norm):
        return [Finding(norm, 0, "filename must never be committed", norm)]
    return []


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------


def _git(*args: str) -> str:
    # S603/S607: `git` is resolved from PATH with a fixed, literal argv built by
    # this module — no shell, no user-supplied executable. check=False because a
    # non-zero git exit (e.g. no commits yet) should yield an empty scan, not a
    # traceback that blocks every commit.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    ).stdout


def scan_staged() -> list[Finding]:
    """Scan only what a commit would add: staged filenames + added diff lines."""
    findings: list[Finding] = []

    staged = [
        p for p in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p
    ]
    for path in staged:
        findings.extend(check_filename(path))

    # The logging-leak rule needs whole-file context (an AST), so parse the
    # staged blob of each staged .py rather than the diff hunks. Only staged
    # files, so this stays cheap.
    for path in staged:
        if not path.endswith(".py") or is_excluded(path):
            continue
        staged_blob = _git("show", f":{path}")
        if staged_blob:
            findings.extend(scan_python_logging(path, staged_blob))

    diff = _git("diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR")
    path = "<unknown>"
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            path = path[2:] if path.startswith("b/") else path
            line_no = 0
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            if path != "/dev/null" and not is_excluded(path):
                findings.extend(scan_line(path, line_no, raw[1:]))
            line_no += 1
    return findings


def scan_tree(root: Path) -> list[Finding]:
    """Scan every tracked file — the safety net when the hook is not installed."""
    findings: list[Finding] = []
    tracked = [p for p in _git("-C", str(root), "ls-files", "-z").split("\0") if p]
    for rel in tracked:
        findings.extend(check_filename(rel))
        if is_excluded(rel):
            continue
        fp = root / rel
        try:
            if not fp.is_file() or fp.stat().st_size > MAX_FILE_BYTES:
                continue
            blob = fp.read_bytes()
        except OSError:
            continue
        if b"\0" in blob:  # binary
            continue
        text = blob.decode("utf-8", errors="replace")
        findings.extend(scan_text(rel, text))
        findings.extend(scan_python_logging(rel, text))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Block credentials from reaching git.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--staged", action="store_true", help="scan the staged diff (pre-commit hook)")
    g.add_argument("--tree", action="store_true", help="scan every tracked file")
    ap.add_argument("--root", default=".", help="repository root")
    args = ap.parse_args(argv)

    # Windows consoles default to cp866/cp1251; findings may quote Russian
    # source lines. Force UTF-8 so the report is readable instead of mojibake.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    findings = scan_staged() if args.staged else scan_tree(Path(args.root).resolve())
    if not findings:
        return 0

    where = "staged changes" if args.staged else "tracked tree"
    print(f"\nSECRET SCAN: {len(findings)} finding(s) in {where}", file=sys.stderr)
    for f in findings:
        print(f.render(), file=sys.stderr)
    print(
        "\nIf this is a REAL secret:\n"
        "  1. unstage it, move the value into .env / PERSONA_DATA_DIR, and\n"
        "  2. ROTATE it — assume anything you staged is already compromised.\n"
        "\nIf this is a false positive, pick one:\n"
        "  * append  # secret-scan: ignore  to that line (preferred — reviewable)\n"
        "  * add the path to EXCLUDED_PATHS in ops/secret_scan.py, with a reason\n"
        "  * one-off bypass:  PERSONA_SKIP_SECRET_SCAN=1 git commit ...\n"
        "See docs/SECRET_HYGIENE.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
