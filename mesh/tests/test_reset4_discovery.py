"""Missing host-pinning cases, exercised through an in-memory discovery pass."""

import pytest

from mesh.discovery import discover_specialists, pin_host, synthesize_lan_status


@pytest.mark.parametrize(
    ("advertised", "peer", "expected"),
    [
        (
            "http://[2001:db8::1]:8003/v1",
            "[2001:db8::2]",
            "http://[2001:db8::2]:8003/v1",
        ),
        (
            "https://advertised.example:8443/v1/a%2Fb?model=a%2Bb&model=c#route",
            "peer.example",
            "https://peer.example:8443/v1/a%2Fb?model=a%2Bb&model=c#route",
        ),
    ],
    ids=["bracketed-ipv6", "encoded-path-query-fragment"],
)
def test_pin_host_and_discovery_preserve_url_components(advertised, peer, expected):
    assert pin_host(advertised, peer) == expected
    calls = []
    payload = {
        "data": [{"id": "synthetic", "routing_meta": {"node_urls": [advertised]}}],
    }

    def fetch(host, port):
        calls.append((host, port))
        return payload

    result = discover_specialists(synthesize_lan_status([peer]), fetch=fetch)

    assert calls == [(peer, 8088)]
    assert result.reachable == [peer]
    assert result.unreachable == []
    assert result.specialists["synthetic"].node_urls == (expected,)
