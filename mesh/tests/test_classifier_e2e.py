"""Real-asset classifier E2E — vendored ONNX embedder + treelite heads + selector.

Needs the full `classifier` extra; skipped otherwise. Assertions are
structural (valid signal vocabulary, selector integration), not semantic
head-accuracy claims — the heads' quality is owned by slancha-api's
training pipeline, not this repo.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("numpy")
pytest.importorskip("treelite")
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")

from mesh.classifier.auto import AutoRouter, prompt_text  # noqa: E402
from mesh.classifier.heads import SignalClassifier  # noqa: E402
from mesh.models import NodeBinding, NodeSummary, RegistrySnapshot, SpecialistCard  # noqa: E402

_DOMAINS = {
    "biology", "business", "chemistry", "computer science", "economics",
    "engineering", "health", "history", "law", "math", "other",
    "philosophy", "physics", "psychology",
    # cluster-head overrides land in catalog vocabulary
    "code", "general",
}
_LANGUAGES = {"en", "es", "fr", "de", "zh", "ja"}


@pytest.fixture(scope="module")
def clf() -> SignalClassifier:
    return SignalClassifier()  # real vendored assets + real embedder


def test_real_pipeline_emits_valid_vocabulary(clf):
    ps = clf.signals_for("Write a Python function that merges two sorted lists.")
    assert ps.signals.domain in _DOMAINS
    assert ps.signals.difficulty in ("easy", "medium", "hard")
    assert ps.signals.language in _LANGUAGES
    assert ps.classifier_ms < 5000  # cold ONNX session load dominates run 1


def test_real_pipeline_is_fast_after_warmup(clf):
    clf.signals_for("warmup")
    ps = clf.signals_for("What year did the French Revolution begin?")
    assert ps.classifier_ms < 500  # embed + 6 heads, CPU


def _snapshot_with(specialist_id: str, domain: str) -> RegistrySnapshot:
    now = datetime.now(timezone.utc)
    binding = NodeBinding(
        node_id="n1",
        specialist_id=specialist_id,
        health="healthy",
        queue_depth=0,
        node_url="http://h:11434",
        last_seen=now,
    )
    card = SpecialistCard(
        model_id="m",
        specialist_id=specialist_id,
        domain=domain,
        difficulty_tiers=["easy", "medium", "hard"],
        required_backend="ollama",
        ollama_tag="t",
        storage_gb=1.0,
        runtime_gb=1.0,
        min_vram_gb=1.0,
        context_window=8192,
        n_layers=8,
        estimated_tps_at={},
    )
    return RegistrySnapshot(
        snapshot_ts=now,
        nodes={"n1": NodeSummary(node_id="n1", friendly_name="n1", health="healthy",
                                 last_seen=now, node_url=binding.node_url)},
        specialists={specialist_id: [binding]},
        catalog={specialist_id: card},
    )


def test_auto_router_selects_from_real_signals(clf):
    """Whole read path: body → prompt_text → classify → select_mesh_route.

    A single general-domain specialist covering every tier catches any
    classified (domain, difficulty) via the selector's fall-through, so
    the pick is deterministic regardless of what the heads say.
    """
    auto = AutoRouter(clf)
    snap = _snapshot_with("gen-1", "general")
    body = {"messages": [{"role": "user", "content": "Tell me about the Roman Empire."}]}
    sel = auto.select(body, snap)
    assert sel.specialist_id == "gen-1"
    assert sel.node_url == "http://h:11434"


def test_prompt_text_extracts_last_user_turn():
    body = {
        "messages": [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": [{"type": "text", "text": "second"}]},
        ]
    }
    assert prompt_text(body) == "second"
    assert prompt_text({}) == ""
    assert prompt_text({"messages": [{"role": "assistant", "content": "x"}]}) == ""
