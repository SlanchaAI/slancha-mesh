"""Synthetic public-API checks for selection gates and stable candidate ties."""

from datetime import datetime, timezone
from itertools import permutations

import pytest

from mesh.models import RegistrySnapshot, Route, SpecialistCard
from mesh.select import ClassifierSignals, PrefVector, select_mesh_route, select_mesh_route_with_pref


def _snapshot(ids=("candidate",)):
    return RegistrySnapshot(
        snapshot_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ranked_routes={"general|medium": [
            Route(
                specialist_id=sid, node_id=f"node-{sid}",
                node_url=f"http://{sid}.invalid/v1", estimated_queue_ms=0,
                p95_latency_ms=600,
            ) for sid in ids
        ]},
        catalog={sid: SpecialistCard(
            specialist_id=sid, model_id=f"model-{sid}", domain="general",
            difficulty_tiers=["medium"], required_backend="vllm",
            storage_gb=1, runtime_gb=1, min_vram_gb=1,
            context_window=8192, n_layers=1, capabilities=["tools", "streaming"],
        ) for sid in ids},
    )


@pytest.mark.parametrize("capabilities", [None, [], ["tools"], ["streaming"], ["tools", "streaming"]])
def test_all_required_capabilities_need_a_present_card(capabilities):
    snap = _snapshot()
    if capabilities is None:
        snap.catalog.clear()
    else:
        snap.catalog["candidate"] = snap.catalog["candidate"].model_copy(
            update={"capabilities": capabilities}
        )
    result = select_mesh_route_with_pref(
        ClassifierSignals("general", "medium"), snap,
        pref=PrefVector(require_capabilities=["tools", "streaming"]),
        cloud_fallback_model="cloud",
    )
    accepted = capabilities == ["tools", "streaming"]
    assert result.cluster_coverage_used is accepted
    assert result.specialist_id == ("candidate" if accepted else None)
    assert result.fallback_chain == [("cloud", None)]


@pytest.mark.parametrize("selector", [select_mesh_route, select_mesh_route_with_pref])
@pytest.mark.parametrize("limit", [0, 17, 2000])
@pytest.mark.parametrize("offset", [0, 1])
def test_queue_limit_is_inclusive(selector, limit, offset):
    snap = _snapshot()
    snap.ranked_routes["general|medium"][0] = snap.ranked_routes["general|medium"][0].model_copy(
        update={"estimated_queue_ms": limit + offset}
    )
    kwargs = {"pref": PrefVector()} if selector is select_mesh_route_with_pref else {}
    result = selector(ClassifierSignals("general", "medium"), snap, max_queue_ms=limit, **kwargs)
    assert result.specialist_id == ("candidate" if offset == 0 else None)
    assert result.cluster_coverage_used is (offset == 0)


@pytest.mark.parametrize("route_class,pref,limit", [
    ("hot_interactive", None, 1500),
    ("hot_interactive", PrefVector(), 1500),
    ("standard", PrefVector(max_latency_ms_p95=800), 800),
])
@pytest.mark.parametrize("offset", [0, 1])
def test_latency_limit_is_inclusive(route_class, pref, limit, offset):
    snap = _snapshot()
    snap.ranked_routes["general|medium"][0] = snap.ranked_routes["general|medium"][0].model_copy(
        update={"p95_latency_ms": limit + offset}
    )
    result = select_mesh_route_with_pref(
        ClassifierSignals("general", "medium", route_class=route_class), snap, pref=pref,
    )
    assert result.specialist_id == ("candidate" if offset == 0 else None)
    assert result.cluster_coverage_used is (offset == 0)


@pytest.mark.parametrize("selector", [select_mesh_route, select_mesh_route_with_pref])
@pytest.mark.parametrize("ids", list(permutations(("zeta", "alpha", "middle"))))
def test_equal_scores_preserve_candidate_order_in_winner_and_fallbacks(selector, ids):
    snap = _snapshot(ids)
    kwargs = {"pref": PrefVector()} if selector is select_mesh_route_with_pref else {}
    for _ in range(2):
        result = selector(
            ClassifierSignals("general", "medium"), snap, cloud_fallback_model="cloud", **kwargs,
        )
        assert (result.model, result.node_id) == (f"model-{ids[0]}", f"node-{ids[0]}")
        assert result.fallback_chain == [
            (f"model-{sid}", f"node-{sid}") for sid in ids[1:]
        ] + [("cloud", None)]
    assert [r.specialist_id for r in snap.ranked_routes["general|medium"]] == list(ids)
