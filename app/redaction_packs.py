"""PII redaction preset packs (one-click install).

The plain :mod:`app.redaction` UI lets operators handcraft regex rules
one at a time. In practice the same five families show up over and over
(banking, credentials, network, etc.), so we ship them as ready-made
*packs*: a single POST inserts every rule in the pack with stable names
and ``INSERT ... ON CONFLICT(name) DO NOTHING`` semantics, so installing
twice is safe and never clobbers a user-edited pattern.

The :data:`CATALOGUE` mapping is the single source of truth for both the
HTML grid and the ``/api/redaction-packs.json`` endpoint. Each pattern
ships with a ``sample_match`` purely for the UI preview — the redaction
worker never reads it.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.redaction_packs")


class PatternSpec(TypedDict):
    """A single regex rule inside a pack."""

    name: str
    regex: str
    sample_match: str


class PackSpec(TypedDict):
    """A named bundle of PII regex rules."""

    title: str
    description: str
    patterns: list[PatternSpec]


CATALOGUE: dict[str, PackSpec] = {
    "banking": {
        "title": "Banking & cards",
        "description": (
            "Mask payment-card numbers, IBAN / BIC codes and "
            "balance lines that frequently leak via banking screenshots."
        ),
        "patterns": [
            {
                "name": "bank_card_16",
                "regex": r"\b(?:\d[ -]*?){13,19}\b",
                "sample_match": "4111 1111 1111 1111",
            },
            {
                "name": "bank_iban",
                "regex": r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b",
                "sample_match": "DE89370400440532013000",
            },
            {
                "name": "bank_bic_swift",
                "regex": r"\b[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?\b",
                "sample_match": "DEUTDEFFXXX",
            },
            {
                "name": "bank_balance_line",
                "regex": (
                    r"(?i)\b(balance|остаток|баланс)\s*[:=]?\s*"
                    r"[\$€£₽]?\s*\d{1,3}(?:[ ,.]\d{3})*(?:[.,]\d{2})?"
                ),
                "sample_match": "Balance: $12,450.30",
            },
            {
                "name": "bank_cvv_hint",
                "regex": r"(?i)\b(cvv|cvc|cvv2|cvc2)\s*[:=]?\s*\d{3,4}\b",
                "sample_match": "CVV: 123",
            },
        ],
    },
    "passwords": {
        "title": "Passwords & secrets",
        "description": (
            "Mask Password: prefixes and common password-manager hints "
            "(1Password, Bitwarden, KeePass, LastPass exports)."
        ),
        "patterns": [
            {
                "name": "pwd_prefix_password",
                "regex": r"(?i)\b(password|пароль|passwd|pwd)\s*[:=]\s*\S+",
                "sample_match": "Password: hunter2",
            },
            {
                "name": "pwd_prefix_pin",
                "regex": r"(?i)\b(pin|pin\s*code|пин|пин-код)\s*[:=]\s*\d{3,8}\b",
                "sample_match": "PIN: 4815",
            },
            {
                "name": "pwd_master_phrase",
                "regex": r"(?i)\b(master\s*password|master\s*passphrase)\s*[:=]\s*\S+",
                "sample_match": "Master Password: correct horse battery",
            },
            {
                "name": "pwd_secret_key",
                "regex": r"(?i)\b(secret\s*key|recovery\s*key)\s*[:=]\s*[A-Z0-9-]{16,}",
                "sample_match": "Secret Key: A3-XXXX-YYYY-ZZZZ-1234-5678-9ABC",
            },
            {
                "name": "pwd_seed_phrase_marker",
                "regex": r"(?i)\b(seed\s*phrase|recovery\s*phrase|mnemonic)\s*[:=]\s*.+",
                "sample_match": "Seed phrase: legal winner thank year wave …",
            },
        ],
    },
    "credentials": {
        "title": "API keys & tokens",
        "description": (
            "Mask common credential shapes: OpenAI sk-keys, AWS access keys, "
            "GitHub personal tokens, Slack xoxb tokens and JWTs."
        ),
        "patterns": [
            {
                "name": "cred_openai_sk",
                "regex": r"\bsk-[A-Za-z0-9_\-]{20,}\b",
                "sample_match": "sk-ABCDEFghijKL0123mnOPqrSTuvwxYZ012345",
            },
            {
                "name": "cred_aws_access_key",
                "regex": r"\bAKIA[0-9A-Z]{16}\b",
                "sample_match": "AKIAIOSFODNN7EXAMPLE",
            },
            {
                "name": "cred_github_token",
                "regex": r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
                "sample_match": "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            },
            {
                "name": "cred_slack_token",
                "regex": r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",
                "sample_match": "xoxb-1234567890-098765432109-AbCdEfGhIjKl",
            },
            {
                "name": "cred_jwt",
                "regex": r"\beyJ[A-Za-z0-9_\-]+?\.[A-Za-z0-9_\-]+?\.[A-Za-z0-9_\-]+\b",
                "sample_match": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
            },
            {
                "name": "cred_bearer_token",
                "regex": r"(?i)bearer\s+[A-Za-z0-9._\-]+",
                "sample_match": "Bearer eyJhbGciOi…",
            },
        ],
    },
    "network": {
        "title": "Network identifiers",
        "description": (
            "Mask raw IPv4 / IPv6 addresses and MAC addresses that surface "
            "in terminals, network dashboards and router admin panels."
        ),
        "patterns": [
            {
                "name": "net_ipv4",
                "regex": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
                "sample_match": "192.168.1.42",
            },
            {
                "name": "net_ipv6",
                "regex": (
                    r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"
                    r"|\b::1\b|\b::\b"
                ),
                "sample_match": "2001:db8:85a3::8a2e:370:7334",
            },
            {
                "name": "net_mac_address",
                "regex": r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b",
                "sample_match": "AA:BB:CC:DD:EE:FF",
            },
            {
                "name": "net_ipv4_cidr",
                "regex": r"\b(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}\b",
                "sample_match": "10.0.0.0/24",
            },
        ],
    },
    "personal": {
        "title": "Personal contacts",
        "description": (
            "Mask personal contact info: email addresses, international "
            "phone numbers (E.164) and Russian phone formats."
        ),
        "patterns": [
            {
                "name": "pii_email",
                "regex": r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
                "sample_match": "alice@example.com",
            },
            {
                "name": "pii_phone_e164",
                "regex": r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,9}",
                "sample_match": "+1 415 555 0102",
            },
            {
                "name": "pii_phone_ru",
                "regex": (
                    r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?"
                    r"\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
                ),
                "sample_match": "+7 (916) 555-12-34",
            },
            {
                "name": "pii_phone_ru_short",
                "regex": r"\b8\d{10}\b",
                "sample_match": "89165551234",
            },
        ],
    },
}


class InstallReport(TypedDict):
    """Return shape of :func:`install_pack`."""

    pack_id: str
    inserted: int
    skipped_duplicate: int


async def install_pack(pack_id: str) -> InstallReport:
    """Install every rule in ``pack_id`` into the ``redaction_rule`` table.

    Uses SQLite's ``INSERT ... ON CONFLICT(name) DO NOTHING`` so already
    installed rules (matched by ``name`` primary key) are skipped without
    raising. A short ``SELECT`` per rule decides whether the row counts
    as ``inserted`` or ``skipped_duplicate`` for the return value — we
    need that split to render a meaningful flash message and we cannot
    rely on ``cursor.rowcount`` across all aiosqlite/SQLite versions.

    Raises :class:`KeyError` if ``pack_id`` is unknown so the calling
    route can map it to an HTTP 404 cleanly.
    """
    if pack_id not in CATALOGUE:
        msg = f"unknown pack: {pack_id}"
        raise KeyError(msg)

    pack = CATALOGUE[pack_id]
    inserted = 0
    skipped = 0

    async with get_connection() as conn:
        for spec in pack["patterns"]:
            name = spec["name"]
            pattern = spec["regex"]
            cursor = await conn.execute(
                "SELECT 1 FROM redaction_rule WHERE name = ?",
                (name,),
            )
            exists = await cursor.fetchone()
            if exists is not None:
                skipped += 1
                continue
            await conn.execute(
                "INSERT INTO redaction_rule (name, pattern, enabled) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(name) DO NOTHING",
                (name, pattern),
            )
            inserted += 1
        await conn.commit()

    log.info(
        "redaction_packs.installed",
        pack_id=pack_id,
        inserted=inserted,
        skipped_duplicate=skipped,
        total=len(pack["patterns"]),
    )
    return {
        "pack_id": pack_id,
        "inserted": inserted,
        "skipped_duplicate": skipped,
    }
