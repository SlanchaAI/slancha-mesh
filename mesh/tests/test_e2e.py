"""End-to-end mesh smoke test — spec §12 day 7 in miniature.

Flow:
  1. Two synthetic Sparks → probe.
  2. allocate_cluster (tiered) → suggestions.
  3. Each node "loads" its suggested specialist (mocked).
  4. Heartbeats hit the registry.
  5. Snapshot + ranked_routes.
  6. Route 5 sample prompts; assert distribution matches coverage.

This is what a real mesh boot looks like in v0.0.2, minus real vLLM.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mesh.allocator import allocate_cluster
from mesh.catalog import load_catalog
from mesh.models import LoadedModel, NodeHeartbeat, NodeUtilization
from mesh.registry import HeartbeatPostRequest, MeshRegistry, build_ranked_routes
from mesh.select import ClassifierSignals, select_mesh_route


def test_e2e_two_spark_mesh(spark_node):
    """Boot a 2-Spark mesh, route 5 prompts, assert distribution."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    catalog = load_catalog()

    # 1. Two synthetic Sparks (identical hardware).
    spark_2 = spark_node.model_copy(update={"node_id": "spark-2", "friendly_name": "spark-2"})
    nodes = [spark_node, spark_2]

    # 2. Allocate tiered.
    suggestions = allocate_cluster(
        nodes,
        catalog,
        traffic_mix={"math": 0.4, "code": 0.4, "general": 0.2},
        strategy="tiered",
    )
    assert all(s.primary is not None for s in suggestions.values()), suggestions
    primary_by_node = {
        nid: s.primary.specialist_id for nid, s in suggestions.items() if s.primary
    }

    # 3. Each node loads its suggested specialist + reports heartbeat.
    reg = MeshRegistry(catalog=catalog)
    for node in nodes:
        spec_id = primary_by_node[node.node_id]
        card = next(c for c in catalog if c.specialist_id == spec_id)
        hb = NodeHeartbeat(
            node_id=node.node_id,
            ts=now,
            hardware=node,
            loaded_models=[
                LoadedModel(
                    specialist_id=spec_id,
                    model_id=card.model_id,
                    loaded_at=now,
                    estimated_tps=60.0,
                )
            ],
            util=NodeUtilization(
                gpu_util_pct=15.0,
                ram_util_pct=35.0,
                queue_depth=0,
                p50_latency_ms_60s=400.0,
                p95_latency_ms_60s=900.0,
            ),
            health="healthy",
        )
        reg.record_heartbeat(
            HeartbeatPostRequest(
                heartbeat=hb, node_url=f"http://{node.friendly_name}:8000/v1"
            )
        )

    # 4. Build snapshot + ranked routes.
    snap = reg.snapshot(now=now)
    snap = snap.model_copy(update={"ranked_routes": build_ranked_routes(snap)})
    assert snap.nodes, "registry snapshot empty after heartbeats"

    # 5. Route 5 sample prompts.
    prompts = [
        ClassifierSignals(domain="math", difficulty="hard"),
        ClassifierSignals(domain="math", difficulty="medium"),
        ClassifierSignals(domain="code", difficulty="medium"),
        ClassifierSignals(domain="code", difficulty="hard"),
        ClassifierSignals(domain="general", difficulty="easy"),
    ]
    results = [select_mesh_route(p, snap) for p in prompts]

    # 6. Assert distribution.
    #
    # Tier-1 allocation on a 2-Spark cluster covers 2 of {math, code,
    # general/reasoning}. So at least 2 of the 5 prompts hit the mesh;
    # the 5th (general, easy) falls through to cloud iff neither node
    # loaded general/reasoning (depends on tiered tie-break).
    mesh_hits = [r for r in results if r.cluster_coverage_used]
    cloud_hits = [r for r in results if not r.cluster_coverage_used]
    assert len(mesh_hits) >= 2, (
        f"expected ≥2 mesh hits, got {len(mesh_hits)}: " + ", ".join(r.reason for r in results)
    )

    # Both mesh nodes should appear in the route distribution (no node
    # starves) when both math and code prompts are present.
    node_ids_hit = {r.node_id for r in mesh_hits}
    domains_loaded = {primary_by_node[nid] for nid in primary_by_node}
    if {"nemotron-math-7b-q4", "qwen3-coder-30b-a3b-fp8"} <= domains_loaded:
        # Both Sparks should be exercised.
        assert len(node_ids_hit) == 2, (
            f"expected both Sparks exercised; hit {node_ids_hit}, "
            f"loaded {primary_by_node}"
        )

    # Every cloud fallback must have non-empty fallback_chain.
    for r in cloud_hits:
        assert r.fallback_chain
        assert r.fallback_chain[-1][1] is None


def test_e2e_event_replay_is_deterministic(spark_node):
    """Re-running the snapshot after the same event log returns the
    same RegistrySnapshot (modulo snapshot_ts)."""
    now = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    catalog = load_catalog()
    reg = MeshRegistry(catalog=catalog)
    hb = NodeHeartbeat(
        node_id=spark_node.node_id,
        ts=now,
        hardware=spark_node,
        loaded_models=[
            LoadedModel(
                specialist_id="nemotron-math-7b-q4",
                model_id="Qwen/Qwen3-Math-7B-Instruct",
                loaded_at=now,
            )
        ],
        util=NodeUtilization(),
        health="healthy",
    )
    reg.record_heartbeat(HeartbeatPostRequest(heartbeat=hb, node_url="http://spark-1:8000/v1"))
    snap1 = reg.snapshot(now=now)
    snap2 = reg.snapshot(now=now)
    assert snap1.model_dump(mode="json") == snap2.model_dump(mode="json")
