"""CI safety net: no credential may live in the tracked tree.

The pre-commit hook (``ops/hooks/pre-commit``) is opt-in — it only protects a
clone where somebody ran ``ops/install_hooks.sh``. This test enforces the same
rule set for everyone, so a secret committed from an un-hooked machine (or
pushed with ``--no-verify``) still turns the suite red before the repo goes
public.

Both the hook and this test call the *same* scanner, ``ops/secret_scan.py``,
so the two can never drift apart.

If this test fails on a genuine secret: unstage/remove it, move the value into
``.env`` or ``PERSONA_DATA_DIR``, and **rotate it** — treat anything that
reached a commit as already compromised.

If it fails on a false positive, in order of preference:
  1. append ``# secret-scan: ignore`` to the offending line;
  2. add the path to ``EXCLUDED_PATHS`` in ``ops/secret_scan.py`` with a reason.

See ``docs/SECRET_HYGIENE.md``.
"""

from __future__ import annotations

import functools
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = REPO_ROOT / "ops" / "secret_scan.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("persona_secret_scan", SCANNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("persona_secret_scan", module)
    spec.loader.exec_module(module)
    return module


def _git_available() -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-dir"],
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


scanner = _load_scanner()


@functools.lru_cache(maxsize=1)
def _tree_findings() -> tuple[object, ...]:
    """Scan the tracked tree once; several tests assert on different slices."""
    return tuple(scanner.scan_tree(REPO_ROOT))


# ---------------------------------------------------------------------------
# The actual guard
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _git_available(), reason="needs a git checkout to enumerate tracked files")
def test_tracked_tree_contains_no_secrets() -> None:
    """Every tracked file is free of credential-shaped strings."""
    findings = _tree_findings()
    assert not findings, "Secrets found in tracked files:\n" + "\n".join(
        f.render() for f in findings
    )


@pytest.mark.skipif(not _git_available(), reason="needs a git checkout to enumerate tracked files")
def test_no_forbidden_filenames_tracked() -> None:
    """No .env / *.pem / *.db / billing_secrets.json is tracked."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    offenders = [p for path in tracked for p in scanner.check_filename(path)]
    assert not offenders, "Files that must never be tracked:\n" + "\n".join(
        f.render() for f in offenders
    )


# ---------------------------------------------------------------------------
# Guard the guard: prove the scanner still detects, so a future over-tuning of
# the regexes (to silence a false positive) cannot quietly disarm it.
# ---------------------------------------------------------------------------

# Fabricated strings with real credential SHAPE and real length. None of these
# authenticate anything; they exist so the assertions below mean something.
# This module is in EXCLUDED_PATHS precisely so it does not flag itself.
MUST_DETECT: list[tuple[str, str]] = [
    ("GitHub PAT (classic)", "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"),
    ("GitHub PAT (fine-grained)", "github_pat_" + "A" * 22 + "_" + "b" * 50),
    ("OpenAI key", "sk-" + "T3BlbkFJ" * 5),
    ("Anthropic key", "sk-ant-api03-" + "Zz9" * 32),
    ("OpenRouter key", "sk-or-v1-" + "0f" * 32),
    ("Groq key", "gsk_" + "Q" * 24 + "9" * 28),
    ("Google API key", "AIza" + "Sy" + "C" * 33),
    ("AWS access key id", "AKIA" + "QYRTZ3XKLMNOPQR2"),
    ("Telegram bot token", "1234567890:AA" + "Ff0" * 11),
    ("YooKassa secret key", "live_MvYQzHwDBRcvOG0mFF2S0kK6qXGZ0RQC9OJEfPd7WgXY"),
    ("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9." + "S" * 32),
    ("PEM private key block", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("credentials embedded in URL", "postgres://admin:Hunt3rSecret99@db.prod.internal:5432/app"),
    ("PERSONA_WORKER_TOKEN assigned a real value", "PERSONA_WORKER_TOKEN=9f3ac21be4d5487fa0c1"),
    # Gmail app password: 4 groups of 4. Spaces must not make it look like prose.
    ("PERSONA_SMTP_PASS assigned a real value", "PERSONA_SMTP_PASS=abcd efgh ijkl mnop"),
    ("PERSONA_ROBOKASSA_PASSWORD1 assigned a real value", "PERSONA_ROBOKASSA_PASSWORD1=xY9kQ2mB"),
    (
        "PERSONA_VAULT_MASTER_PASSWORD assigned a real value",
        "PERSONA_VAULT_MASTER_PASSWORD=Tr0ub4dor&3xK",
    ),
]


@pytest.mark.parametrize("label,sample", MUST_DETECT, ids=[label for label, _ in MUST_DETECT])
def test_scanner_detects_known_secret_shapes(label: str, sample: str) -> None:
    findings = scanner.scan_line("some_new_file.py", 1, sample)
    assert findings, f"scanner no longer detects {label!r} - a regex was weakened"
    assert label in {f.label for f in findings}


# Strings that look secret-ish but are not, drawn from real false positives
# this repo produced. Keeps the rules from being tightened into noise.
MUST_NOT_DETECT: list[str] = [
    "def test_dedup_collapses_near_duplicate_frames() -> None:",  # long snake_case
    "PERSONA_BYO_API_KEY=sk-ant-...",  # elided placeholder in docs
    "PERSONA_SMTP_PASS=",  # empty in .env.example
    "PERSONA_WORKER_TOKEN=${TOKEN}",  # shell interpolation
    "PERSONA_YOOKASSA_SHOP_ID=<your-shop-id>",  # angle-bracket placeholder
    # Usage docstrings in scripts/encrypted_backup.py: a quoted English phrase
    # and an elided value, both ending in a shell line-continuation.
    "    PERSONA_BACKUP_PASSPHRASE='at least twelve chars' \\\\",
    "    PERSONA_BACKUP_PASSPHRASE='...' \\\\",
    'url = "https://user:secret@example.com"',  # documentation URL
    'raise ConnectionError("https://bot-token:secret@example.invalid")',
    'secret = os.environ.get("PERSONA_YOOKASSA_SECRET_KEY", "")',  # correct loading
    r'_PEM_KEY = re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")',  # a detector
    "AKIAIOSFODNN7EXAMPLE  # secret-scan: ignore",  # inline escape hatch honoured
]


@pytest.mark.parametrize("sample", MUST_NOT_DETECT)
def test_scanner_ignores_known_false_positives(sample: str) -> None:
    findings = scanner.scan_line("some_new_file.py", 1, sample)
    assert not findings, f"false positive on {sample!r}: {[f.label for f in findings]}"


# ---------------------------------------------------------------------------
# .env.example completeness
#
# An undocumented secret env var is how secrets end up hardcoded: the next
# person cannot find where the value is supposed to go, so they inline it.
# ---------------------------------------------------------------------------

# Env-var names whose *shape* says "this holds a credential".
_SECRET_SHAPED = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSPHRASE|_PASS$|_PASS_|APIKEY|API_KEY|API_HASH"
    r"|API_ID|_KEY$|_KEY_|CREDENTIAL|_HASH$|SESSION_STRING|_LOGIN$)"
)

# Prefixes we care about: this project's own vars plus the provider vars it
# reads directly.
_ENV_NAME = re.compile(
    r"\b((?:PERSONA|BRAVE|OPENAI|ANTHROPIC|OPENROUTER|GROQ|MISTRAL|XAI|TOGETHER"
    r"|AWS|GITHUB|GH)_[A-Z0-9_]{2,})\b"
)

# Names that match the shape but are NOT secrets. Each needs a reason.
_NOT_ACTUALLY_SECRET: dict[str, str] = {
    "PERSONA_SESSION_IDLE_DAYS": "session lifetime in days, not a session token",
    "PERSONA_AGENT_CONFIG": "path to a config file",
    "PERSONA_API_KEY_HEADER": "the header NAME to read a key from",
}

_SOURCE_SUFFIXES = (".py", ".ps1", ".sh", ".bat")


def _secret_env_names_used_in_code() -> dict[str, str]:
    """Map secret-shaped env var name -> first file that mentions it."""
    listing = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "app", "ops", "scripts", "mac-agent"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    found: dict[str, str] = {}
    for rel in sorted(listing):
        if not rel.endswith(_SOURCE_SUFFIXES):
            continue
        try:
            text = (REPO_ROOT / rel).read_text("utf-8", errors="replace")
        except OSError:
            continue
        for match in _ENV_NAME.finditer(text):
            name = match.group(1)
            if not _SECRET_SHAPED.search(name) or name in _NOT_ACTUALLY_SECRET:
                continue
            found.setdefault(name, rel)
    return found


@pytest.mark.skipif(not _git_available(), reason="needs a git checkout to enumerate sources")
def test_env_example_documents_every_secret_variable() -> None:
    """Every secret-shaped env var read by the code appears in .env.example."""
    documented = (REPO_ROOT / ".env.example").read_text("utf-8", errors="replace")
    used = _secret_env_names_used_in_code()

    missing = {name: src for name, src in used.items() if name not in documented}
    assert not missing, (
        "Secret env vars used in code but absent from .env.example:\n"
        + "\n".join(f"  {name}  (first seen in {src})" for name, src in sorted(missing.items()))
        + "\n\nAdd each with an EMPTY value and a one-line comment saying what it "
        "is and where to obtain it."
    )


def test_env_example_has_no_real_secret_values() -> None:
    """.env.example is a template: every secret-shaped key must be empty."""
    offenders: list[str] = []
    for line_no, line in enumerate(
        (REPO_ROOT / ".env.example").read_text("utf-8", errors="replace").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if _SECRET_SHAPED.search(key.strip()) and value.strip():
            offenders.append(f"  .env.example:{line_no}: {key.strip()} is not empty")
    assert not offenders, "\n".join(offenders)


# ---------------------------------------------------------------------------
# Logging leaks — the 6a806dc bug class (a credential written to the log)
# ---------------------------------------------------------------------------

_LEAKY_LOG_SAMPLES = [
    'logger.error("share_corrupt", token=token, error=str(exc))',
    'log.info("worker.auth", secret=secret)',
    'log.warning("smtp.failed", password=password)',
    'log.debug(f"issuing {api_key} to caller")',
    'log.info("dev.paired", device_token=device.device_token)',
    # Locally-named logger. Matching only bare `log`/`logger` let two real
    # leaks through a `cover_log` binding in share_collection.py.
    'cover_log.error("share_cover_set", token=token, cover_shot_id=cover_id)',
    'audit_logger.info("issued", secret=secret)',
]

# Shapes that look similar but log no secret. All are real lines from this
# codebase — the rule must stay quiet on them or it will simply be bypassed.
_SAFE_LOG_SAMPLES = [
    'log.info("api_token.dep.reject", reason="missing")',
    'log.info("api_token.revoked", token_id=token_id)',
    'log.info("auth.user.password_changed", user_id=user_id)',
    'log.info("device.token_rotated", device_id=device_id, user_id=user_id)',
    'log.warning("vault.set.invalid", key=key, has_password=bool(password))',
    'log.info("telegram.worker.disabled", reason="missing_bot_token")',
    'log.info("hashtag_suggest.no_tokens", shot_id=shot_id)',
    'log.info("llm.usage", max_tokens=max_tokens, prompt_tokens=prompt_tokens)',
    'fp_map = {d["id"]: _token_fingerprint(d["device_token"]) for d in devices}',
]


@pytest.mark.parametrize("sample", _LEAKY_LOG_SAMPLES)
def test_detects_credential_written_to_log(sample: str) -> None:
    findings = scanner.scan_python_logging("app/some_new_route.py", sample)
    assert findings, f"logging leak not detected: {sample}"


@pytest.mark.parametrize("sample", _SAFE_LOG_SAMPLES)
def test_ignores_safe_logging(sample: str) -> None:
    findings = scanner.scan_python_logging("app/some_new_route.py", sample)
    assert not findings, f"false positive on safe logging: {sample} -> {findings}"


@pytest.mark.skipif(not _git_available(), reason="needs a git checkout to enumerate sources")
def test_no_new_logging_leaks() -> None:
    """No logging leak beyond the documented baseline.

    Subset check on purpose: fixing a baselined occurrence keeps this green,
    while introducing a new one turns it red.
    """
    leaks = [f for f in _tree_findings() if "logging call leaks a credential" in f.label]
    assert not leaks, (
        "New logging call(s) writing a credential to the log:\n"
        + "\n".join(f.render() for f in leaks)
        + "\n\nLog a fingerprint or hash instead of the value."
    )


def test_logging_leak_baseline_entries_are_documented() -> None:
    """Baselined leaks need a reason, and the file must still exist."""
    for key, reason in scanner.KNOWN_LOGGING_LEAKS.items():
        path, _, event = key.partition(":")
        assert event, f"baseline key {key!r} must be '<path>:<log event name>'"
        assert reason.strip(), f"baseline entry {key!r} needs a reason"
        assert (REPO_ROOT / path).exists(), (
            f"baseline lists {path!r}, which no longer exists - remove the entry"
        )


@pytest.mark.skipif(not _git_available(), reason="needs a checkout")
def test_logging_leak_baseline_has_no_stale_entries() -> None:
    """A baselined leak that has been fixed must be removed from the baseline.

    Without this, a stale entry keeps suppressing its key forever — which is
    exactly how the first version of this baseline hid two live leaks behind a
    already-fixed entry.
    """
    still_leaking: set[str] = set()
    for key in scanner.KNOWN_LOGGING_LEAKS:
        path, _, _event = key.partition(":")
        source = (REPO_ROOT / path).read_text("utf-8", errors="replace")
        for finding in scanner.scan_python_logging(path, source, apply_baseline=False):
            if f"[{key}]" in finding.excerpt:
                still_leaking.add(key)

    stale = set(scanner.KNOWN_LOGGING_LEAKS) - still_leaking
    assert not stale, (
        "KNOWN_LOGGING_LEAKS entries that no longer leak (fixed - delete them):\n"
        + "\n".join(f"  {k}" for k in sorted(stale))
    )


def test_not_actually_secret_exclusions_have_reasons() -> None:
    for name, reason in _NOT_ACTUALLY_SECRET.items():
        assert reason.strip(), f"_NOT_ACTUALLY_SECRET[{name!r}] needs a reason"


def test_every_path_exclusion_has_a_reason_and_still_exists() -> None:
    """An exclusion for a deleted file is dead weight that hides the next leak."""
    for path, reason in scanner.EXCLUDED_PATHS.items():
        assert reason.strip(), f"EXCLUDED_PATHS[{path!r}] needs a reason"
        assert (REPO_ROOT / path).exists(), (
            f"EXCLUDED_PATHS lists {path!r}, which no longer exists - remove the entry"
        )
