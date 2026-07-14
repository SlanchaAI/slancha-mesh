"""Router `model:"auto"` wiring — the auto_router seam in create_router_app.

The classifier itself is exercised in test_classifier_heads.py; here a
fake AutoRouterLike pins the contract between the router and any
resolver implementation:

  - "auto" + auto_router → resolve, proxy to the picked specialist,
    audit headers name the resolved id,
  - "auto" without auto_router → 404 with the install hint,
  - resolver falls through to cloud (specialist_id=None) → 503,
  - explicit specialist_id never touches the resolver,
  - `/v1/models` lists "auto" iff the seam is wired.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from mesh.models import (
    MeshSelectionResult,
    NodeBinding,
    NodeSummary,
    RegistrySnapshot,
    SpecialistCard,
)
from mesh.router_app import create_router_app


def _card(specialist_id: str, backend: str = "ollama", domain: str = "code") -> SpecialistCard:
    return SpecialistCard(
        model_id="Qwen/Qwen2.5-Coder-7B-Instruct",
        specialist_id=specialist_id,
        domain=domain,
        difficulty_tiers=["medium"],
        required_backend=backend,
        ollama_tag="qwen2.5-coder:7b" if backend == "ollama" else None,
        storage_gb=5.0,
        runtime_gb=6.0,
        min_vram_gb=8.0,
        context_window=32768,
        n_layers=28,
        estimated_tps_at={"gb10": 40.0},
    )


def _snapshot(
    specialist_id: str, backend: str = "ollama", domain: str = "code"
) -> RegistrySnapshot:
    now = datetime.now(timezone.utc)
    binding = NodeBinding(
        node_id="node-a",
        specialist_id=specialist_id,
        health="healthy",
        queue_depth=0,
        p95_latency_ms_60s=420.0,
        node_url="http://10.0.0.5:11434",
        last_seen=now,
    )
    return RegistrySnapshot(
        snapshot_ts=now,
        nodes={
            "node-a": NodeSummary(
                node_id="node-a",
                friendly_name="node-a",
                health="healthy",
                last_seen=now,
                loaded_specialist_ids=[specialist_id],
                node_url=binding.node_url,
            )
        },
        specialists={specialist_id: [binding]},
        catalog={specialist_id: _card(specialist_id, backend, domain)},
    )


def _selection(specialist_id: str | None) -> MeshSelectionResult:
    return MeshSelectionResult(
        model="Qwen/Qwen2.5-Coder-7B-Instruct" if specialist_id else "cloud-fallback",
        specialist_id=specialist_id,
        node_id="node-a" if specialist_id else None,
        node_url="http://10.0.0.5:11434" if specialist_id else None,
        reason="fake selection" if specialist_id else "no mesh route for general/hard",
        queue_ms_estimated=0,
        cluster_coverage_used=specialist_id is not None,
    )


class _FakeAutoRouter:
    def __init__(self, result: MeshSelectionResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def select(self, body: dict, snapshot: RegistrySnapshot) -> MeshSelectionResult:
        self.calls.append(body)
        return self.result


def _client(
    snapshot: RegistrySnapshot,
    auto_router: _FakeAutoRouter | None,
) -> tuple[TestClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
    app = create_router_app(
        snapshot_source=lambda: snapshot,
        http_client=upstream,
        auto_router=auto_router,
    )
    return TestClient(app), seen


_BODY = {"model": "auto", "messages": [{"role": "user", "content": "write a sort function"}]}


def test_auto_resolves_and_proxies_to_selected_specialist():
    sid = "qwen2.5-coder-7b-q4-ollama"
    fake = _FakeAutoRouter(_selection(sid))
    client, seen = _client(_snapshot(sid), fake)

    resp = client.post("/v1/chat/completions", json=_BODY)

    assert resp.status_code == 200
    assert len(fake.calls) == 1  # resolver consulted exactly once
    assert resp.headers["X-Slancha-Specialist"] == sid
    # Upstream got the ollama_tag rewrite, same as an explicit-id request.
    import json

    assert json.loads(seen[0].content)["model"] == "qwen2.5-coder:7b"


def test_auto_never_leaks_auto_upstream_for_vllm_passthrough():
    """vLLM/external cards skip the ollama_tag rewrite — the upstream must
    still receive the resolved specialist_id, never the literal "auto"
    (vLLM serves under --served-model-name=specialist_id and 404s on
    unknown ids)."""
    sid = "phi-4-14b-q4"
    fake = _FakeAutoRouter(_selection(sid))
    client, seen = _client(_snapshot(sid, backend="vllm"), fake)

    resp = client.post("/v1/chat/completions", json=_BODY)

    assert resp.status_code == 200
    import json

    assert json.loads(seen[0].content)["model"] == sid


def test_auto_streaming_resolves_before_proxying():
    """The auto branch sits above the stream/non-stream fork — stream:true
    must resolve identically and carry the resolved model upstream."""
    sid = "phi-4-14b-q4"
    fake = _FakeAutoRouter(_selection(sid))
    client, seen = _client(_snapshot(sid, backend="vllm"), fake)

    resp = client.post("/v1/chat/completions", json={**_BODY, "stream": True})

    assert resp.status_code == 200
    assert len(fake.calls) == 1
    import json

    assert json.loads(seen[0].content)["model"] == sid


def test_auto_without_auto_router_is_404_with_hint():
    sid = "qwen2.5-coder-7b-q4-ollama"
    client, seen = _client(_snapshot(sid), auto_router=None)

    resp = client.post("/v1/chat/completions", json=_BODY)

    assert resp.status_code == 404
    assert "--auto-route" in resp.json()["detail"]
    assert seen == []  # nothing proxied


def test_auto_cloud_fallthrough_is_503_with_reason():
    sid = "qwen2.5-coder-7b-q4-ollama"
    fake = _FakeAutoRouter(_selection(None))
    client, seen = _client(_snapshot(sid), fake)

    resp = client.post("/v1/chat/completions", json=_BODY)

    assert resp.status_code == 503
    assert "no mesh route" in resp.json()["detail"]
    assert seen == []


def test_explicit_specialist_id_never_consults_resolver():
    sid = "qwen2.5-coder-7b-q4-ollama"
    fake = _FakeAutoRouter(_selection(sid))
    client, _ = _client(_snapshot(sid), fake)

    resp = client.post(
        "/v1/chat/completions",
        json={"model": sid, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert fake.calls == []


def test_empty_prompt_short_circuits_to_general_medium():
    """No user text → deterministic general/medium routing, classifier
    never invoked (AutoRouter is importable without the heavy extra, so
    this runs everywhere)."""
    from mesh.classifier.auto import AutoRouter

    sid = "phi-4-mini-q4-ollama"
    snap = _snapshot(sid, domain="general")
    auto = AutoRouter(classifier=None)  # would explode if consulted
    sel = auto.select({"messages": [{"role": "system", "content": "be terse"}]}, snap)
    assert sel.specialist_id == sid


def test_models_lists_auto_only_when_wired():
    sid = "qwen2.5-coder-7b-q4-ollama"
    snap = _snapshot(sid)

    with_auto, _ = _client(snap, _FakeAutoRouter(_selection(sid)))
    ids = [m["id"] for m in with_auto.get("/v1/models").json()["data"]]
    assert ids == ["auto", sid]

    without_auto, _ = _client(snap, None)
    ids = [m["id"] for m in without_auto.get("/v1/models").json()["data"]]
    assert ids == [sid]
