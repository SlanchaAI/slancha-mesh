"""Per-node identity cert + registry pinning (#102)."""

from __future__ import annotations

import pytest

pytest.importorskip("nacl.signing")  # identity needs PyNaCl (the 'signing' extra)

from mesh.identity import (  # noqa: E402
    NodeIdentityError,
    build_node_cert,
    did_for,
    generate_node_keypair,
    verify_node_cert,
)
from mesh.registry import HeartbeatPostRequest, MeshRegistry  # noqa: E402
from mesh.tests.conftest import make_heartbeat  # noqa: E402


# ── identity module ──────────────────────────────────────────────────────────
def test_keypair_cert_roundtrip():
    sk, pk = generate_node_keypair()
    cert = build_node_cert("node-a", sk)
    assert cert["node_id"] == "node-a" and cert["public_key_b64"] == pk
    assert verify_node_cert(cert, "node-a") is True
    assert did_for("node-a", pk).startswith("did:wire:node-a-")


def test_verify_rejects_tamper_and_mismatch():
    sk, _ = generate_node_keypair()
    cert = build_node_cert("node-a", sk)
    assert verify_node_cert(cert, "node-b") is False          # wrong expected id
    assert verify_node_cert(dict(cert, node_id="node-b"), "node-b") is False  # tampered id
    sk2, pk2 = generate_node_keypair()                         # another key can't vouch for node-a
    forged = {"node_id": "node-a", "public_key_b64": pk2, "signature_b64": cert["signature_b64"]}
    assert verify_node_cert(forged, "node-a") is False
    assert verify_node_cert({}, "node-a") is False
    assert verify_node_cert("nope", "node-a") is False


# ── registry pinning ─────────────────────────────────────────────────────────
def _req(node, fresh_now, catalog, cert=None, url="http://n:8000/v1"):
    hb = make_heartbeat(node, fresh_now, [], catalog)
    return HeartbeatPostRequest(heartbeat=hb, node_url=url, identity_cert=cert)


def test_pins_node_id_and_refuses_impersonation(spark_node, catalog, fresh_now):
    nid = spark_node.node_id
    sk, pk = generate_node_keypair()
    reg = MeshRegistry(catalog=catalog)
    reg.record_heartbeat(_req(spark_node, fresh_now, catalog, build_node_cert(nid, sk)))
    assert reg._node_pubkeys[nid] == pk
    # a DIFFERENT key claiming the same node_id is refused (the impersonation fix)
    sk2, _ = generate_node_keypair()
    with pytest.raises(NodeIdentityError, match="pinned"):
        reg.record_heartbeat(_req(spark_node, fresh_now, catalog, build_node_cert(nid, sk2)))


def test_certless_after_pinned_is_downgrade(spark_node, catalog, fresh_now):
    nid = spark_node.node_id
    sk, _ = generate_node_keypair()
    reg = MeshRegistry(catalog=catalog)
    reg.record_heartbeat(_req(spark_node, fresh_now, catalog, build_node_cert(nid, sk)))
    with pytest.raises(NodeIdentityError, match="downgrade"):
        reg.record_heartbeat(_req(spark_node, fresh_now, catalog, cert=None))


def test_require_identity_rejects_certless(spark_node, catalog, fresh_now):
    reg = MeshRegistry(catalog=catalog, require_node_identity=True)
    with pytest.raises(NodeIdentityError, match="required"):
        reg.record_heartbeat(_req(spark_node, fresh_now, catalog, cert=None))


def test_invalid_cert_rejected(spark_node, catalog, fresh_now):
    nid = spark_node.node_id
    sk, _ = generate_node_keypair()
    cert = build_node_cert(nid, sk)
    cert["signature_b64"] = "AAAA"  # corrupt the signature
    reg = MeshRegistry(catalog=catalog)
    with pytest.raises(NodeIdentityError, match="invalid"):
        reg.record_heartbeat(_req(spark_node, fresh_now, catalog, cert))


def test_certless_default_path_unaffected(spark_node, catalog, fresh_now):
    """Back-compat: no cert + not required + never pinned → accepted."""
    reg = MeshRegistry(catalog=catalog)
    resp = reg.record_heartbeat(_req(spark_node, fresh_now, catalog, cert=None))
    assert resp.ack is True


class _MemStore:
    """Durable EventStore fake: keeps appended envelopes so a fresh MeshRegistry
    built on the same store replays them — i.e. simulates a process restart."""

    def __init__(self):
        self.envelopes = []

    def append(self, env):
        self.envelopes.append(env)

    def replay(self):
        return list(self.envelopes)


def test_pin_survives_registry_restart(spark_node, catalog, fresh_now):
    """H3 (#102): the identity pin must be rebuilt from the durable log on
    restart. Before the fix, `_node_pubkeys` was reset empty on boot (the cert
    was never persisted), so an attacker could re-pin a victim's node_id to
    their own key after any crash/deploy, and a previously-authenticated node
    could be silently downgraded."""
    nid = spark_node.node_id
    sk, pk = generate_node_keypair()
    store = _MemStore()

    reg1 = MeshRegistry(catalog=catalog, store=store)
    reg1.record_heartbeat(_req(spark_node, fresh_now, catalog, build_node_cert(nid, sk)))
    assert reg1._node_pubkeys[nid] == pk

    # "Restart": a new registry on the same durable store.
    reg2 = MeshRegistry(catalog=catalog, store=store)
    assert reg2._node_pubkeys.get(nid) == pk  # pin rebuilt from the log

    # And it's enforced: a different key is refused (no re-pin window)...
    sk2, _ = generate_node_keypair()
    with pytest.raises(NodeIdentityError, match="pinned"):
        reg2.record_heartbeat(_req(spark_node, fresh_now, catalog, build_node_cert(nid, sk2)))
    # ...and a cert-less heartbeat is still a refused downgrade.
    with pytest.raises(NodeIdentityError, match="downgrade"):
        reg2.record_heartbeat(_req(spark_node, fresh_now, catalog, cert=None))


# ── #108 peer challenge (sign/verify) ────────────────────────────────────────
def test_challenge_sign_verify():
    from mesh.identity import sign_challenge, verify_challenge

    sk, pk = generate_node_keypair()
    cr = sign_challenge("peer-1", sk, "nonce123", "2026-01-01T00:00:00Z")
    assert verify_challenge(cr, expected_nonce="nonce123") == ("peer-1", pk)
    assert verify_challenge(cr, expected_nonce="OTHER") is None          # replay/wrong nonce
    assert verify_challenge(dict(cr, signature_b64="AAAA"), expected_nonce="nonce123") is None
    assert verify_challenge(dict(cr, node_id="evil"), expected_nonce="nonce123") is None  # id swap breaks sig
    assert verify_challenge({}, expected_nonce="nonce123") is None


# ── #108 discovery client verification + TOFU pin ────────────────────────────
class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_discovery_verifies_and_pins_peer(monkeypatch):
    import httpx

    from mesh.discovery import make_http_fetch
    from mesh.identity import sign_challenge

    sk, _ = generate_node_keypair()

    def fake_get(url, params=None, headers=None, timeout=None):
        cr = sign_challenge("peer-1", sk, headers["X-Mesh-Challenge"], headers["X-Mesh-Challenge-Ts"])
        return _Resp({"object": "list", "data": [], "challenge_response": cr})

    monkeypatch.setattr(httpx, "get", fake_get)
    fetch = make_http_fetch(verify_peers=True)
    assert fetch("10.0.0.5", 8003) is not None   # verified
    assert fetch("10.0.0.5", 8003) is not None   # same key, still trusted (pinned)


def test_discovery_drops_unverifiable_peer(monkeypatch):
    import httpx

    from mesh.discovery import make_http_fetch

    # peer returns no challenge_response at all
    monkeypatch.setattr(httpx, "get",
                        lambda *a, **k: _Resp({"object": "list", "data": []}))
    assert make_http_fetch(verify_peers=True)("10.0.0.5", 8003) is None
    # but with verification OFF, the same response is accepted (back-compat)
    assert make_http_fetch(verify_peers=False)("10.0.0.5", 8003) is not None


def test_discovery_drops_peer_whose_key_changes(monkeypatch):
    import httpx

    from mesh.discovery import make_http_fetch
    from mesh.identity import sign_challenge

    sk1, _ = generate_node_keypair()
    sk2, _ = generate_node_keypair()
    keys = [sk1, sk2]

    def fake_get(url, params=None, headers=None, timeout=None):
        cr = sign_challenge("peer-1", keys.pop(0), headers["X-Mesh-Challenge"],
                            headers["X-Mesh-Challenge-Ts"])
        return _Resp({"object": "list", "data": [], "challenge_response": cr})

    monkeypatch.setattr(httpx, "get", fake_get)
    fetch = make_http_fetch(verify_peers=True)
    assert fetch("10.0.0.5", 8003) is not None   # first call pins key1
    assert fetch("10.0.0.5", 8003) is None       # key changed → MITM/impersonation → dropped


def test_models_endpoint_signs_challenge_when_peer_key_set(monkeypatch):
    from fastapi.testclient import TestClient

    from mesh.identity import generate_node_keypair as _gk, verify_challenge
    from mesh.registry_app import create_mesh_app

    sk, pk = _gk()
    monkeypatch.setenv("SLANCHA_PEER_KEY_B64", sk)
    monkeypatch.setenv("SLANCHA_PEER_NODE_ID", "peer-1")
    monkeypatch.delenv("SLANCHA_NODE_TOKEN", raising=False)
    client = TestClient(create_mesh_app())
    r = client.get("/models", headers={"X-Mesh-Challenge": "abc", "X-Mesh-Challenge-Ts": "t0"})
    cr = r.json()["challenge_response"]
    assert verify_challenge(cr, expected_nonce="abc") == ("peer-1", pk)
    # no challenge header → no challenge_response (opt-in)
    assert "challenge_response" not in client.get("/models").json()
