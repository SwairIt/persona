"""Scoped credential for the outbound-only browser worker.

The browser worker is intentionally isolated from the LLM worker credential:
possession of one token must never authorize the other capability.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

_KV_BROWSER_TOKEN_HASH = "remote_browser_worker_token_hash"  # noqa: S105


async def rotate_browser_worker_token() -> str:
    """Rotate the browser-only token and return its plaintext exactly once."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with get_connection() as conn:
        await set_kv(conn, _KV_BROWSER_TOKEN_HASH, digest)
    return token


async def validate_browser_worker_token(token: str) -> bool:
    """Return whether ``token`` matches the browser-only credential."""
    if not token:
        return False
    async with get_connection() as conn:
        stored = await get_kv(conn, _KV_BROWSER_TOKEN_HASH)
    if not stored:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, stored)


__all__ = [
    "rotate_browser_worker_token",
    "validate_browser_worker_token",
]
