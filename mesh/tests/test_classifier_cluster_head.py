"""Cluster-head selector (7th head) — the safe-by-default guardrails.

Fake ClusterHead impls exercise the selector without treelite. The three
guardrails from the port source (slancha-local): inert on any load
failure, confidence-gated predict, sidecar-mapped cluster→cap.
"""

from __future__ import annotations

import json

import pytest

np = pytest.importorskip("numpy")

from mesh.classifier.cluster_head import (  # noqa: E402 — after importorskip
    DEFAULT_CONFIDENCE_THRESHOLD,
    ClusterHeadSelector,
    ClusterRouteHint,
    load_from_dir,
)

def _x():
    return np.zeros((1, 512), dtype=np.float32)


class _FakeHead:
    def __init__(self, cid: int, conf: float) -> None:
        self._out = (cid, conf)

    def predict(self, x):
        return self._out


def test_confident_mapped_prediction_yields_hint():
    sel = ClusterHeadSelector(_FakeHead(3, 0.9), {3: "coding"}, head_version="v7")
    hint = sel.predict(_x())
    assert hint == ClusterRouteHint(cluster_id=3, cap="coding", confidence=0.9, head_version="v7")
    assert "v=v7" in hint.reason()


def test_below_threshold_returns_none():
    sel = ClusterHeadSelector(_FakeHead(3, DEFAULT_CONFIDENCE_THRESHOLD - 0.01), {3: "coding"})
    assert sel.predict(_x()) is None


def test_unmapped_cluster_id_returns_none():
    sel = ClusterHeadSelector(_FakeHead(9, 0.99), {3: "coding"})
    assert sel.predict(_x()) is None


def test_env_threshold_override(monkeypatch):
    monkeypatch.setenv("SLANCHA_CLUSTER_HEAD_CONF_THRESHOLD", "0.5")
    sel = ClusterHeadSelector(_FakeHead(3, 0.6), {3: "math"})
    assert sel.predict(_x()) is not None


def test_invalid_env_threshold_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SLANCHA_CLUSTER_HEAD_CONF_THRESHOLD", "not-a-float")
    sel = ClusterHeadSelector(_FakeHead(3, 0.69), {3: "math"})
    assert sel.predict(_x()) is None  # default 0.7 still gates


# ---------------------------------------------------------------------------
# load_from_dir — every failure path is inert (None), never a raise
# ---------------------------------------------------------------------------


def test_load_from_dir_none_or_empty_is_inert():
    assert load_from_dir(None) is None
    assert load_from_dir("") is None


def test_load_from_dir_missing_bin_is_inert(tmp_path):
    assert load_from_dir(tmp_path) is None


def test_load_from_dir_bad_sidecar_is_inert(tmp_path):
    (tmp_path / "mmbert_tl_cluster.bin").write_bytes(b"not-a-model")
    (tmp_path / "cluster_id_to_route.json").write_text("{not json")
    assert load_from_dir(tmp_path) is None


def test_load_from_dir_wrong_schema_version_is_inert(tmp_path):
    (tmp_path / "mmbert_tl_cluster.bin").write_bytes(b"not-a-model")
    (tmp_path / "cluster_id_to_route.json").write_text(
        json.dumps({"schema_version": "v2", "routes": {"0": "coding"}})
    )
    assert load_from_dir(tmp_path) is None


def test_load_from_dir_garbage_bin_is_inert(tmp_path):
    """Valid sidecar + corrupt .bin → treelite load fails → inert, no raise."""
    (tmp_path / "mmbert_tl_cluster.bin").write_bytes(b"not-a-model")
    (tmp_path / "cluster_id_to_route.json").write_text(
        json.dumps({"schema_version": "v1", "routes": {"0": "coding"}})
    )
    assert load_from_dir(tmp_path) is None
