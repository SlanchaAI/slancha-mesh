"""SSRF guard for node-supplied node_url (#98)."""

from __future__ import annotations

import socket
from unittest.mock import Mock

import pytest

from mesh.url_guard import NodeUrlError, validate_node_url


@pytest.mark.parametrize("url", [
    "http://node-a.ts.net:8003",        # MagicDNS name
    "https://10.0.0.5:8004/v1",          # private LAN
    "http://192.168.1.20:11434",         # private LAN
    "http://100.64.0.9:8003",            # tailnet CGNAT
    "http://127.0.0.1:11434",            # loopback — legit single-box (allowed by default)
])
def test_legit_node_urls_pass(url):
    assert validate_node_url(url) == url


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud IMDS (link-local)
    "http://169.254.169.254:80/",
    "file:///etc/passwd",                          # non-http scheme
    "gopher://internal/",
    "ftp://10.0.0.1/",
    "http://0.0.0.0/",                             # unspecified
    "http://[::]/",
    "no-scheme-host",
])
def test_ssrf_and_bad_scheme_rejected(url):
    with pytest.raises(NodeUrlError):
        validate_node_url(url)


@pytest.mark.parametrize("url", [
    "http://2852039166/",                  # decimal int form of 169.254.169.254 (IMDS)
    "http://0xA9.0xFE.0xA9.0xFE/",          # hex-octet form of 169.254.169.254
    "http://0xA9FEA9FE/",                    # single hex form of 169.254.169.254
    "http://[::ffff:169.254.169.254]/",     # IPv4-mapped IPv6 → link-local
])
def test_obfuscated_imds_evasions_rejected(url):
    """Decimal/hex/octal/mapped encodings of the cloud metadata IP that an HTTP
    client still dials must be canonicalized + blocked (SSRF evasion)."""
    with pytest.raises(NodeUrlError):
        validate_node_url(url)


def test_ipv6_loopback_symmetric_with_ipv4(monkeypatch):
    """::1 is loopback like 127.0.0.1 — allowed by default (was wrongly rejected as
    'reserved'), and blocked by the SAME flag."""
    assert validate_node_url("http://[::1]:8003/") == "http://[::1]:8003/"
    monkeypatch.setenv("SLANCHA_NODE_URL_BLOCK_LOOPBACK", "1")
    with pytest.raises(NodeUrlError):
        validate_node_url("http://[::1]:8003/")


def test_short_form_loopback_canonicalized(monkeypatch):
    """127.1 → 127.0.0.1: a short-form loopback follows the loopback policy, not
    silently treated as an opaque hostname."""
    assert validate_node_url("http://127.1:8003/") == "http://127.1:8003/"
    monkeypatch.setenv("SLANCHA_NODE_URL_BLOCK_LOOPBACK", "1")
    with pytest.raises(NodeUrlError):
        validate_node_url("http://127.1:8003/")


def test_block_loopback_when_flag_set(monkeypatch):
    assert validate_node_url("http://127.0.0.1:8003")  # default: allowed
    monkeypatch.setenv("SLANCHA_NODE_URL_BLOCK_LOOPBACK", "1")
    with pytest.raises(NodeUrlError):
        validate_node_url("http://127.0.0.1:8003")


def test_block_private_when_flag_set(monkeypatch):
    assert validate_node_url("http://10.0.0.5:8003")  # default: allowed
    monkeypatch.setenv("SLANCHA_NODE_URL_BLOCK_PRIVATE", "1")
    with pytest.raises(NodeUrlError):
        validate_node_url("http://10.0.0.5:8003")


def test_loaded_model_ingest_rejects_poisoned_node_url():
    """The node-supplied ingest model refuses a poisoned node_url at the boundary."""
    from pydantic import ValidationError

    from mesh.models import LoadedModel

    base = dict(specialist_id="qwen2.5-coder-7b-q4-ollama", model_id="qwen2.5-coder:7b",
                loaded_at="2026-06-03T00:00:00Z")
    LoadedModel(**base, node_url="http://node-a.ts.net:8003")          # valid passes
    LoadedModel(**base, node_url=None)                                  # None ok
    for bad in ("http://169.254.169.254/", "file:///etc/passwd"):
        with pytest.raises(ValidationError):
            LoadedModel(**base, node_url=bad)


@pytest.fixture
def dns_guard(monkeypatch):
    monkeypatch.setenv("SLANCHA_NODE_URL_RESOLVE_DNS", "1")
    monkeypatch.delenv("SLANCHA_NODE_URL_BLOCK_LOOPBACK", raising=False)
    monkeypatch.delenv("SLANCHA_NODE_URL_BLOCK_PRIVATE", raising=False)
    resolver = Mock()
    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return resolver


def _answers(*ips):
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
             socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 8003)) for ip in ips]


@pytest.mark.parametrize("ip", [
    "169.254.169.254", "fe80::1", "0.0.0.0", "::", "224.0.0.1", "ff02::1",
    "240.0.0.1", "100::1", "::ffff:169.254.169.254",
])
@pytest.mark.parametrize("first", [True, False])
def test_dns_rejects_any_unsafe_answer(dns_guard, ip, first):
    ips = [ip, "8.8.8.8"] if first else ["8.8.8.8", ip]
    dns_guard.return_value = _answers(*ips)
    with pytest.raises(NodeUrlError):
        validate_node_url("http://node.example:8003/v1")


@pytest.mark.parametrize("ip,flag", [
    ("127.0.0.1", "LOOPBACK"), ("::1", "LOOPBACK"),
    ("::ffff:127.0.0.1", "LOOPBACK"),
    ("10.0.0.1", "PRIVATE"), ("fd00::1", "PRIVATE"),
])
def test_dns_uses_existing_flags(dns_guard, monkeypatch, ip, flag):
    dns_guard.return_value = _answers(ip, "8.8.8.8")
    url = "http://node.example:8003/v1"
    assert validate_node_url(url) == url
    monkeypatch.setenv(f"SLANCHA_NODE_URL_BLOCK_{flag}", "1")
    with pytest.raises(NodeUrlError):
        validate_node_url(url)


@pytest.mark.parametrize("url,port", [
    ("http://node.example/v1", 80), ("https://node.example/v1", 443),
    ("http://node.example:8003/v1", 8003),
])
def test_dns_public_answers_and_port(dns_guard, url, port):
    dns_guard.return_value = _answers("8.8.8.8", "2606:4700:4700::1111")
    assert validate_node_url(url) == url
    dns_guard.assert_called_once_with("node.example", port, type=socket.SOCK_STREAM)


@pytest.mark.parametrize("url", [
    "http://node.ts.net", "http://NODE.TS.NET.:8003", "http://127.1:8003",
    "http://[::1]", "http://100.64.0.9", "http://8.8.8.8",
])
def test_dns_exempt_hosts_do_not_resolve(dns_guard, url):
    assert validate_node_url(url) == url
    dns_guard.assert_not_called()


def test_dns_default_does_not_resolve(dns_guard, monkeypatch):
    monkeypatch.delenv("SLANCHA_NODE_URL_RESOLVE_DNS")
    assert validate_node_url("http://node.example") == "http://node.example"
    dns_guard.assert_not_called()


@pytest.mark.parametrize("host", ["ts.net.attacker.example", "notts.net", "ts.net"])
def test_dns_tailnet_exemption_is_suffix_bounded(dns_guard, host):
    dns_guard.return_value = _answers("169.254.169.254")
    with pytest.raises(NodeUrlError):
        validate_node_url(f"http://{host}")


@pytest.mark.parametrize("result", [[], socket.gaierror("no address")])
def test_dns_resolution_failure_rejected(dns_guard, result):
    if isinstance(result, Exception):
        dns_guard.side_effect = result
    else:
        dns_guard.return_value = result
    with pytest.raises(NodeUrlError, match="resolved"):
        validate_node_url("http://node.example")


def test_probe_redirect_rechecks_dns_at_dial(dns_guard):
    from urllib.error import HTTPError
    from urllib.request import Request

    from mesh.quality_probe import _SsrfGuardedRedirect

    url = "http://node.example:8003/v1"
    dns_guard.side_effect = [_answers("8.8.8.8"), _answers("169.254.169.254")]
    assert validate_node_url(url) == url
    with pytest.raises(HTTPError, match="unsafe redirect blocked"):
        _SsrfGuardedRedirect().redirect_request(Request(url), None, 302, "Found", {}, url)
    assert dns_guard.call_count == 2
