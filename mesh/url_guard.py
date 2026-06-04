"""SSRF guard for node-supplied ``node_url`` (issue #98).

A node reports its OpenAI-compatible base URL in its heartbeat; the router and the
quality probe later make real HTTP requests to it. Unvalidated, a poisoned
``node_url`` turns the control plane into an SSRF engine — the crown-jewel target
being the cloud metadata endpoint ``http://169.254.169.254/`` (IAM creds), plus
``file://`` reads through the probe's urllib client.

Deployment-aware policy (mesh nodes legitimately run on tailnet / LAN / loopback
addresses, so a blanket private-IP block would break the normal topology):

  ALWAYS rejected:
    • non-``http``/``https`` schemes  → kills ``file://`` / ``gopher://`` etc.
    • link-local IP literals (169.254.0.0/16, fe80::/10) → the cloud IMDS vector
    • unspecified (0.0.0.0/::), multicast, reserved IP literals
  ALLOWED by default (legit node addresses):
    • loopback (single-box: router + node on 127.0.0.1) and private/CGNAT
      ranges (LAN 10/172.16/192.168, tailnet 100.64.0.0/10)

  Tighten for hardened multi-tenant cloud via env:
    • ``SLANCHA_NODE_URL_BLOCK_LOOPBACK=1``  — also reject 127.0.0.0/8 / ::1
    • ``SLANCHA_NODE_URL_BLOCK_PRIVATE=1``   — also reject RFC-1918 / CGNAT

DNS names are allowed (MagicDNS ``*.ts.net``, LAN hostnames) — they are not
resolved here (cheap, no network, no resolve-time TOCTOU); DNS-rebinding defense
is a documented follow-up.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit


class NodeUrlError(ValueError):
    """A node_url is unsafe to dial (bad scheme or a non-routable host)."""


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _as_ip(host: str) -> ipaddress._BaseAddress | None:
    """Parse ``host`` to an IP, CANONICALIZING the obfuscated IPv4 forms that an
    HTTP client / the OS resolver will still dial — the classic SSRF evasions that
    a plain ``ip_address(host)`` misses because it only accepts dotted-quad:

        http://2852039166/         (decimal int)   → 169.254.169.254
        http://0xA9.0xFE.0xA9.0xFE (hex octets)    → 169.254.169.254
        http://127.1/              (short form)    → 127.0.0.1
        http://0177.0.0.1/         (octal)         → 127.0.0.1

    ``socket.inet_aton`` accepts exactly these legacy numeric forms and rejects a
    genuine hostname (→ OSError), so we use it to normalize before the IP checks.
    Returns None for a real DNS name (allowed, not resolved here)."""
    try:
        return ipaddress.ip_address(host)  # dotted-quad IPv4 or any IPv6 literal
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except OSError:
        return None  # a DNS name (MagicDNS / LAN hostname)


def validate_node_url(url: str) -> str:
    """Return ``url`` unchanged if safe to dial, else raise ``NodeUrlError``."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise NodeUrlError(f"node_url scheme must be http/https, got {parts.scheme or '(none)'!r}")
    host = parts.hostname
    if not host:
        raise NodeUrlError("node_url must include a host")
    ip = _as_ip(host)
    if ip is None:
        return url  # a DNS name — allowed, not resolved here (DNS-rebinding is a documented follow-up)
    # An IPv4-mapped IPv6 literal (::ffff:a.b.c.d) is judged by its embedded IPv4,
    # so a mapped link-local/loopback can't slip past the IPv6 property checks.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    # Loopback FIRST + symmetrically for IPv4 (127/8) and IPv6 (::1): allowed by
    # default (single-box router+node), blockable via the flag. (Previously ::1 was
    # rejected as 'reserved' while 127.0.0.1 was allowed — an IPv4/IPv6 asymmetry.)
    if ip.is_loopback:
        if _flag("SLANCHA_NODE_URL_BLOCK_LOOPBACK"):
            raise NodeUrlError(f"node_url host {host!r} is loopback (blocked by SLANCHA_NODE_URL_BLOCK_LOOPBACK)")
        return url
    if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        raise NodeUrlError(f"node_url host {host!r} is not a routable node address (link-local/IMDS/reserved)")
    if ip.is_private and _flag("SLANCHA_NODE_URL_BLOCK_PRIVATE"):
        raise NodeUrlError(f"node_url host {host!r} is private (blocked by SLANCHA_NODE_URL_BLOCK_PRIVATE)")
    return url


__all__ = ["validate_node_url", "NodeUrlError"]
