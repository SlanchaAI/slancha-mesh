"""SignalClassifier — 6 vendored heads → ClassifierSignals (+ 7th-head override).

Needs the `classifier` extra's treelite (heads deserialize from the
vendored assets); the ONNX embedder is stubbed via the `embed_fn` seam so
onnxruntime stays optional here. test_classifier_e2e.py covers the real
embedder path.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("treelite")

from mesh.classifier.cluster_head import ClusterHeadSelector  # noqa: E402
from mesh.classifier.heads import PromptSignals, SignalClassifier  # noqa: E402

EMBED_DIM = 512  # mmBERT-small hidden_size; heads take the pooled 512-dim vector


def _stub_embed(text: str):
    rng = np.random.default_rng(abs(hash(text)) % (2**32))
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture(scope="module")
def clf() -> SignalClassifier:
    return SignalClassifier(embed_fn=_stub_embed)


def test_signals_shape_is_valid_for_select_mesh_route(clf):
    ps = clf.signals_for("write a python quicksort")
    assert isinstance(ps, PromptSignals)
    assert ps.signals.difficulty in ("easy", "medium", "hard")
    assert isinstance(ps.signals.domain, str) and ps.signals.domain
    assert isinstance(ps.signals.needs_tools, bool)
    assert isinstance(ps.jailbreak, bool)
    assert isinstance(ps.pii, bool)
    assert ps.classifier_ms >= 0.0
    assert ps.cluster_hint is None  # no 7th head wired


class _FixedHead:
    def __init__(self, cid: int, conf: float) -> None:
        self._out = (cid, conf)

    def predict(self, x):
        return self._out


def test_cluster_override_rewrites_domain(clf):
    sel = ClusterHeadSelector(_FixedHead(0, 0.99), {0: "coding"}, head_version="test")
    boosted = SignalClassifier(embed_fn=_stub_embed, cluster_head_selector=sel)
    ps = boosted.signals_for("anything")
    assert ps.signals.domain == "code"  # sidecar cap "coding" → catalog "code"
    assert ps.cluster_hint is not None and "cid=0" in ps.cluster_hint


def test_cluster_unknown_cap_is_ignored(clf):
    sel = ClusterHeadSelector(_FixedHead(0, 0.99), {0: "quantum"}, head_version="test")
    boosted = SignalClassifier(embed_fn=_stub_embed, cluster_head_selector=sel)
    baseline = clf.signals_for("anything")
    ps = boosted.signals_for("anything")
    assert ps.signals.domain == baseline.signals.domain  # untouched
    assert ps.cluster_hint is None


def test_low_confidence_cluster_head_is_inert(clf):
    sel = ClusterHeadSelector(_FixedHead(0, 0.1), {0: "coding"}, head_version="test")
    boosted = SignalClassifier(embed_fn=_stub_embed, cluster_head_selector=sel)
    baseline = clf.signals_for("anything")
    ps = boosted.signals_for("anything")
    assert ps.signals.domain == baseline.signals.domain
    assert ps.cluster_hint is None
