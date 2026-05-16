"""Live vLLM integration test — guarded by VLLM_LIVE_URL env var.

This test is the v0.0.2 acceptance criterion: classify → mesh select →
real OpenAI-compatible call to a live vLLM serving a Qwen3 specialist
on the local Spark.

Run with:
    VLLM_LIVE_URL=http://127.0.0.1:8001 \
        uv run pytest mesh/tests/test_integration_vllm.py -v

The test is **skipped** when VLLM_LIVE_URL is unset so the unit suite
stays hermetic. Keep it that way — there's no value in spinning up a
4-minute vLLM in CI.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

import pytest

from mesh.backends import VLLMBackend
from mesh.catalog import load_catalog
from mesh.models import (
    LoadedModel,
    NodeHeartbeat,
    NodeUtilization,
)
from mesh.probe import probe_node
from mesh.registry import HeartbeatPostRequest, MeshRegistry
from mesh.select import ClassifierSignals, select_mesh_route

LIVE_URL = os.environ.get("VLLM_LIVE_URL")
LIVE_SPECIALIST = os.environ.get(
    "VLLM_LIVE_SPECIALIST", "qwen3-coder-30b-a3b-fp8"
)

requires_live_vllm = pytest.mark.skipif(
    LIVE_URL is None,
    reason="VLLM_LIVE_URL not set; skipping live vLLM integration",
)


@requires_live_vllm
def test_live_vllm_health_endpoint_responds():
    """Cheapest check that vLLM is alive."""
    req = urllib.request.Request(f"{LIVE_URL}/health")
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200


@requires_live_vllm
def test_live_vllm_serves_chat_completion_and_measures_tps():
    """Real chat completion + tok/s measurement. Logged for the build doc."""
    payload = {
        "model": LIVE_SPECIALIST,
        "messages": [
            {
                "role": "user",
                "content": "Write a Python one-liner that reverses a string.",
            }
        ],
        "max_tokens": 80,
        "temperature": 0.2,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{LIVE_URL}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        assert resp.status == 200
        data = json.loads(resp.read())
    elapsed = time.time() - t0

    assert data["choices"][0]["message"]["content"].strip()
    out_tokens = data["usage"]["completion_tokens"]
    tps = out_tokens / elapsed if elapsed > 0 else 0
    # Record for human inspection — pytest -v shows this in the test name
    print(f"\n  live: {out_tokens} tokens in {elapsed:.2f}s = {tps:.1f} tok/s")
    # Sanity: at least 1 tok/s. Real benchmarking happens in the build doc.
    assert tps > 1.0, f"unreasonably slow: {tps} tok/s"


@requires_live_vllm
def test_live_vllm_metrics_parseable():
    """The heartbeat reads `/metrics`. If vLLM stops emitting Prometheus
    text, our `_parse_vllm_metrics` returns zeros — silent regression.
    We assert at least one expected gauge name appears in the body so we
    catch a vLLM rename early.
    """
    req = urllib.request.Request(f"{LIVE_URL}/metrics")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    # vLLM 0.17 names these gauges:
    assert "vllm:num_requests_waiting" in body or "vllm:num_requests_running" in body


@requires_live_vllm
def test_live_vllm_end_to_end_via_mesh_router():
    """Full path: ClassifierSignals → select_mesh_route → POST to vLLM.

    This is the moneyshot of v0.0.2: prove the mesh's routing layer can
    drive a real inference call without any test-only short-circuits.
    """
    catalog = load_catalog()
    by_id = {c.specialist_id: c for c in catalog}
    card = by_id[LIVE_SPECIALIST]

    # Build a heartbeat with the live backend's URL
    probe = probe_node()
    backend = VLLMBackend(card=card, host="127.0.0.1", port=int(LIVE_URL.rsplit(":", 1)[-1]))
    util = backend.utilization()
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

    registry = MeshRegistry(catalog=[card])
    hb = NodeHeartbeat(
        node_id=probe.node_id,
        ts=now,
        hardware=probe,
        loaded_models=[
            LoadedModel(
                specialist_id=card.specialist_id,
                model_id=card.model_id,
                loaded_at=now,
                estimated_tps=card.estimated_tps_at.get("gb10"),
            )
        ],
        util=NodeUtilization(queue_depth=int(util.get("queue_depth", 0))),
        health="healthy",
    )
    registry.record_heartbeat(HeartbeatPostRequest(heartbeat=hb, node_url=LIVE_URL))
    snap = registry.snapshot()

    # Router decision
    result = select_mesh_route(
        signals=ClassifierSignals(domain="code", difficulty="medium"),
        registry_snapshot=snap,
    )
    assert result.cluster_coverage_used is True
    assert result.specialist_id == card.specialist_id
    assert result.node_url == LIVE_URL
    print(f"\n  router → {result.specialist_id} @ {result.node_url}")
    print(f"  reason: {result.reason}")

    # Actually invoke the chosen route
    payload = {
        "model": result.specialist_id,
        "messages": [{"role": "user", "content": "def fib(n):\n    "}],
        "max_tokens": 40,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        f"{result.node_url}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    elapsed = time.time() - t0
    content = body["choices"][0]["message"]["content"]
    completion_tokens = body["usage"]["completion_tokens"]
    tps = completion_tokens / elapsed
    print(f"  served: {completion_tokens} tokens in {elapsed:.2f}s = {tps:.1f} tok/s")
    print(f"  content[:200]: {content[:200]!r}")
    assert completion_tokens > 0
