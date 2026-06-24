"""Shared SSRF guard — reject URLs that resolve to non-public addresses.

A single :func:`url_is_safe` predicate, used by every outbound-HTTP caller
that takes a *user-supplied* destination (webhook dispatch, the CSV
pipeline). It parses the URL, requires an ``http``/``https`` scheme, resolves
the host via :func:`socket.getaddrinfo` and refuses the request if ANY
resolved address is private / loopback / link-local / reserved / multicast /
unspecified. That blocks the classic SSRF pivots — ``127.0.0.1``, RFC1918
ranges, ``169.254.169.254`` (cloud metadata), ``::1`` and friends — so a
public-looking webhook URL cannot be used to reach the server's own network.

Fail-closed: a malformed URL, a non-http(s) scheme, a missing host, or a DNS
lookup that fails all return ``False``. Dropping a delivery is the safe
outcome; letting an unverifiable host through is not.

Note this is a best-effort, point-in-time check (DNS can change between the
check and the actual connect — "DNS rebinding"). Callers must ALSO disable
HTTP redirect-following so a vetted public host cannot ``30x`` the request
into an internal address after the fact.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

__all__ = ["url_is_safe"]


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ``ip`` is anything other than a routable public address."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def url_is_safe(url: str) -> bool:
    """Return True only if ``url`` is http(s) and resolves to public IP(s).

    Returns ``False`` for a malformed URL, a non-http(s) scheme, a missing
    host, a DNS-resolution failure, or ANY resolved address that is private,
    loopback, link-local, reserved, multicast or unspecified (so the function
    blocks ``127.0.0.1``, RFC1918, ``169.254.169.254``, ``::1`` …).
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip()
    if not host:
        return False

    # Literal IP? Short-circuit the DNS round-trip and check it directly.
    # ``hostname`` already strips the ``[]`` from an IPv6 literal, but be
    # defensive in case a caller passes the bracketed form.
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        return not _ip_is_blocked(literal)

    # Hostname → resolve and check EVERY A/AAAA record. A single internal
    # answer is enough to reject (defends against a host that returns one
    # public and one private address).
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Fail closed: if we cannot resolve it, we cannot prove it is safe.
        return False
    if not infos:
        return False
    for *_meta, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if _ip_is_blocked(ip):
            return False
    return True
