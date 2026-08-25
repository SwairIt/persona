"""Trusted reverse-proxy peers — who is allowed to speak ``X-Forwarded-For``.

Why this exists
---------------
``X-Forwarded-For`` is a *client-supplied* header. Anything that trusts it
blindly can be spoofed by the client itself. Two security controls in Persona
read the "real" client IP:

* the per-IP auth rate limiter (``app.web.routes.auth._rate_limited``) — a
  spoofable IP means unlimited login/registration attempts;
* the ЮKassa webhook IP allow-list (``app.web.routes.billing``) — a spoofable
  IP means anyone can forge a "payment succeeded" callback.

So XFF is honoured **only** when the direct TCP peer is a known reverse proxy.

Configuration
-------------
Default: ``127.0.0.1`` (loopback, e.g. a local nginx / devtunnel client) and
``192.168.33.3`` (the FastPanel reverse proxy in front of the yesbeat VPS).
These are the values that were hard-coded before this module existed; they stay
the default so nothing changes on deploy.

Override, highest precedence first:

1. env ``PERSONA_TRUSTED_PROXIES`` — comma/space separated list of IPs or CIDR
   blocks, e.g. ``PERSONA_TRUSTED_PROXIES="127.0.0.1,10.0.0.0/8"``.
2. kv ``trusted_proxies`` — same syntax, editable at runtime without a restart
   (60 s cache). Only consulted when the env var is unset.
3. the built-in default above.

Setting either to the literal ``none`` (or an empty string via env) disables XFF
trust entirely — the direct peer is always used.

Verifying against the live proxy
--------------------------------
The application logs, **once per process**, a warning
``auth.proxy.untrusted_xff`` naming the peer that sent an XFF header without
being trusted. To check the real deployment:

1. ``curl -H 'X-Forwarded-For: 1.2.3.4' https://<site>/auth/login`` from the
   public internet, then grep the app log for ``auth.proxy.untrusted_xff``.
   * The warning appears with ``peer=<the proxy's internal IP>`` → that IP is
     the real front-end and must be added to the list.
   * No warning appears → the peer is already trusted; the list is correct.
2. Cross-check with the proxy's own config, e.g. on the VPS
   ``ip -4 addr`` on the FastPanel/nginx host, or
   ``ss -tnp | grep :8000`` on the app host to see who connects to uvicorn.

Never guess an IP: an over-broad list re-opens exactly the spoofing hole this
module closes. When unsure, keep the default and read the warning.

Fail-safe posture
-----------------
Any parse error, DB error or unexpected value falls back to the built-in
default set (deny-by-default for everything else) — never to "trust everyone".
"""

from __future__ import annotations

import ipaddress
import os
import time

from app.logging_setup import get_logger

log = get_logger("persona.auth.proxies")

__all__ = [
    "DEFAULT_TRUSTED_PROXIES",
    "is_trusted_peer",
    "note_untrusted_xff",
    "prime",
    "reset_cache",
    "trusted_networks_sync",
    "trusted_proxies",
]

ENV_VAR = "PERSONA_TRUSTED_PROXIES"
KV_KEY = "trusted_proxies"

#: Values hard-coded in ``app/web/routes/auth.py`` before this module existed.
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = ("127.0.0.1", "192.168.33.3")

_TTL = 60.0
_cache: dict[str, object] = {"value": None, "checked_at": 0.0}

# "Have we already shouted about an untrusted peer sending XFF?" — one warning
# per process, keyed by peer so a second *different* peer is still reported.
_warned_peers: set[str] = set()
_WARN_CAP = 20  # bound memory if someone sprays spoofed peers


def _parse(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse a comma/space separated list of IPs or CIDRs into networks.

    Unparseable entries are skipped with a warning rather than raising — one
    typo in an env var must not brick the process at import time.
    """
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        item = chunk.strip()
        if not item or item.lower() == "none":
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("auth.proxy.bad_entry", entry=item)
    return tuple(nets)


def _default_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return _parse(",".join(DEFAULT_TRUSTED_PROXIES))


async def _from_kv() -> str | None:
    """Read kv ``trusted_proxies``. Any failure → ``None`` (use the default)."""
    try:
        # Local import: this module is imported from the request hot path and
        # must not drag storage in at auth-package import time.
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            raw = await get_kv(conn, KV_KEY)
    except Exception as exc:  # noqa: BLE001 — never let config lookup break auth
        log.debug("auth.proxy.kv_read_failed", error=str(exc))
        return None
    value = (raw or "").strip()
    return value or None


def _env_override() -> str | None:
    """Env wins over kv. An env var set to "" means "trust nobody"."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return None
    return raw  # may legitimately be "" → empty trust set


async def trusted_proxies() -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """Return the currently configured trusted-proxy networks (60 s cache)."""
    now = time.monotonic()
    cached = _cache["value"]
    if cached is not None and now - float(_cache["checked_at"]) < _TTL:  # type: ignore[arg-type]
        return cached  # type: ignore[return-value]

    raw = _env_override()
    source = "env"
    if raw is None:
        raw = await _from_kv()
        source = "kv"
    if raw is None:
        nets = _default_networks()
        source = "default"
    else:
        nets = _parse(raw)

    _cache["value"] = nets
    _cache["checked_at"] = now
    log.debug(
        "auth.proxy.resolved",
        source=source,
        networks=[str(n) for n in nets],
    )
    return nets


def is_trusted_peer_sync(
    peer: str,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    """Membership test with no I/O — used by callers that already resolved."""
    if not peer or peer == "unknown":
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in networks)


async def is_trusted_peer(peer: str) -> bool:
    """True when ``peer`` (the direct TCP peer) may set X-Forwarded-For."""
    return is_trusted_peer_sync(peer, await trusted_proxies())


def trusted_networks_sync() -> tuple[
    ipaddress.IPv4Network | ipaddress.IPv6Network, ...
]:
    """Synchronous view of the trusted set — for callers that cannot await.

    ``app.web.routes.auth._client_ip`` is sync (and is imported as such by the
    billing webhook), so it cannot read kv itself. This returns the cache that
    :func:`prime` keeps warm from the middleware, falling back to env/default
    before the first prime. Never returns "everything".
    """
    cached = _cache["value"]
    if cached is not None:
        return cached  # type: ignore[return-value]
    raw = _env_override()
    return _parse(raw) if raw is not None else _default_networks()


async def prime() -> None:
    """Refresh the cached trusted-proxy set. Cheap (60 s TTL); never raises."""
    try:
        await trusted_proxies()
    except Exception as exc:  # noqa: BLE001 — config refresh must never 500
        log.debug("auth.proxy.prime_failed", error=str(exc))


def note_untrusted_xff(peer: str, path: str) -> None:
    """Log — loudly, once per peer — that an untrusted host sent XFF.

    A single line at WARNING is enough to catch "the proxy IP changed and rate
    limiting silently degraded to per-proxy". Per-request logging here would be
    a log-flood amplifier, so the peer is remembered in-process.
    """
    if peer in _warned_peers:
        return
    if len(_warned_peers) >= _WARN_CAP:
        return
    _warned_peers.add(peer)
    log.warning(
        "auth.proxy.untrusted_xff",
        peer=peer,
        path=path,
        hint=(
            "X-Forwarded-For arrived from a peer that is NOT in "
            f"{ENV_VAR}/kv:{KV_KEY}. The header is being IGNORED (rate limits "
            "and the billing webhook IP filter now key on the peer itself). "
            "If this peer IS your reverse proxy, add it to the list."
        ),
    )


def reset_cache() -> None:
    """Drop the cached configuration and the warn-once set (tests / kv edits)."""
    _cache["value"] = None
    _cache["checked_at"] = 0.0
    _warned_peers.clear()
