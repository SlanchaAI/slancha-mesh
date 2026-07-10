"""Router selection tests — spec §6.

Cover 3 cluster configurations:
1. Single Spark hosting math+code+general (1-node setup).
2. Two Sparks, math on one, code on the other (diversified).
3. Empty cluster — must fall through to cloud.
"""

from __future__ import annotations

from mesh.registry import HeartbeatPostRequest, MeshRegistry, build_ranked_routes
from mesh.select import ClassifierSignals, select_mesh_route
from mesh.tests.conftest import make_heartbeat


def _make_snapshot(spark_node, loaded_ids, catalog, fresh_now, queue_depth=0, p95=600.0):
    reg = MeshRegistry(catalog=catalog)
    hb = make_heartbeat(
        spark_node, fresh_now, loaded_ids, catalog, queue_depth=queue_depth, p95_ms=p95
    )
    reg.record_heartbeat(
        HeartbeatPostRequest(heartbeat=hb, node_url="http://spark-1:8000/v1")
    )
    snap = reg.snapshot(now=fresh_now)
    return snap.model_copy(update={"ranked_routes": build_ranked_routes(snap)})


def test_router_picks_math_specialist_for_math_prompt(spark_node, catalog, fresh_now):
    snap = _make_snapshot(spark_node, ["nemotron-math-7b-q4"], catalog, fresh_now)
    signals = ClassifierSignals(domain="math", difficulty="hard")
    result = select_mesh_route(signals, snap)
    assert result.specialist_id == "nemotron-math-7b-q4"
    assert result.node_id == spark_node.node_id
    assert result.cluster_coverage_used is True


def test_router_falls_through_to_cloud_when_no_mesh_node(catalog, fresh_now):
    """Empty registry → cloud fallback."""
    reg = MeshRegistry(catalog=catalog)
    snap = reg.snapshot(now=fresh_now)
    signals = ClassifierSignals(domain="math", difficulty="hard")
    result = select_mesh_route(signals, snap)
    assert result.cluster_coverage_used is False
    assert result.node_id is None
    assert "cloud" in result.reason.lower()


def test_router_domain_fallthrough_to_general(spark_node, catalog, fresh_now):
    """Cluster has general but no math → math prompt falls through to
    general (acceptable degradation, better than cloud)."""
    snap = _make_snapshot(spark_node, ["ministral-3-8b-q4"], catalog, fresh_now)
    # llama-3.1-8b only declares ["easy","medium"]; we ask for math/easy,
    # router maps to "math" domain, no math route → falls through to
    # general|easy which IS loaded.
    signals = ClassifierSignals(domain="math", difficulty="easy")
    result = select_mesh_route(signals, snap)
    assert result.cluster_coverage_used is True
    assert result.specialist_id == "ministral-3-8b-q4"


def test_router_multilingual_with_no_multilingual_specialist(spark_node, catalog, fresh_now):
    """language=es + general only → multilingual key, no fallback path
    that handles non-en → cloud."""
    snap = _make_snapshot(spark_node, ["ministral-3-8b-q4"], catalog, fresh_now)
    # Domain explicitly multilingual, no multilingual specialist loaded.
    # Difficulty=hard so general|easy/medium fall-through doesn't catch.
    signals = ClassifierSignals(domain="multilingual", difficulty="hard", language="es")
    result = select_mesh_route(signals, snap)
    assert result.cluster_coverage_used is False
    assert result.node_id is None


def test_router_diversified_two_node_cluster(spark_node, catalog, fresh_now):
    """Two Sparks: one loads math, one loads code. Math prompts go to
    math node; code prompts go to code node."""
    spark_2 = spark_node.model_copy(
        update={"node_id": "spark-2", "friendly_name": "spark-2"}
    )
    reg = MeshRegistry(catalog=catalog)
    reg.record_heartbeat(
        HeartbeatPostRequest(
            heartbeat=make_heartbeat(spark_node, fresh_now, ["nemotron-math-7b-q4"], catalog),
            node_url="http://spark-1:8000/v1",
        )
    )
    reg.record_heartbeat(
        HeartbeatPostRequest(
            heartbeat=make_heartbeat(spark_2, fresh_now, ["qwen3-coder-30b-a3b-fp8"], catalog),
            node_url="http://spark-2:8000/v1",
        )
    )
    snap = reg.snapshot(now=fresh_now)
    snap = snap.model_copy(update={"ranked_routes": build_ranked_routes(snap)})

    math_r = select_mesh_route(ClassifierSignals(domain="math", difficulty="hard"), snap)
    code_r = select_mesh_route(ClassifierSignals(domain="code", difficulty="medium"), snap)
    assert math_r.node_id == "spark-1"
    assert code_r.node_id == "spark-2"
    assert math_r.node_url == "http://spark-1:8000/v1"
    assert code_r.node_url == "http://spark-2:8000/v1"


def test_router_queue_filter_drops_overloaded_nodes(spark_node, catalog, fresh_now):
    """Queue depth 10 → 5000ms estimate → above 2000ms default → dropped."""
    snap = _make_snapshot(
        spark_node, ["nemotron-math-7b-q4"], catalog, fresh_now, queue_depth=10
    )
    result = select_mesh_route(
        ClassifierSignals(domain="math", difficulty="hard"), snap
    )
    assert result.cluster_coverage_used is False


def test_router_hot_interactive_p95_filter(spark_node, catalog, fresh_now):
    """p95 latency 3000ms exceeds 1500ms hot_interactive budget → dropped."""
    snap = _make_snapshot(spark_node, ["nemotron-math-7b-q4"], catalog, fresh_now, p95=3000.0)
    result = select_mesh_route(
        ClassifierSignals(domain="math", difficulty="hard", route_class="hot_interactive"),
        snap,
    )
    assert result.cluster_coverage_used is False
    # Same prompt at standard route_class should succeed
    result_std = select_mesh_route(
        ClassifierSignals(domain="math", difficulty="hard", route_class="standard"),
        snap,
    )
    assert result_std.cluster_coverage_used is True


def test_router_fallback_chain_present(spark_node, catalog, fresh_now):
    """Even with a successful mesh pick, fallback_chain ends with cloud."""
    snap = _make_snapshot(spark_node, ["nemotron-math-7b-q4"], catalog, fresh_now)
    result = select_mesh_route(
        ClassifierSignals(domain="math", difficulty="hard"), snap
    )
    assert result.fallback_chain
    assert result.fallback_chain[-1][1] is None  # cloud terminus
