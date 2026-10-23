"""Dependency-light prompt extraction through the real auto-routing selector."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mesh.classifier.auto import AutoRouter, prompt_text
from mesh.models import NodeBinding, RegistrySnapshot, SpecialistCard
from mesh.select import ClassifierSignals


@pytest.fixture
def snapshot():
    now = datetime.now(timezone.utc)
    cards = {}
    bindings = {}
    for domain in ("code", "general"):
        sid = f"synthetic-{domain}"
        cards[sid] = SpecialistCard(
            model_id=sid, specialist_id=sid, domain=domain,
            difficulty_tiers=["medium"], required_backend="ollama",
            ollama_tag=sid, storage_gb=1, runtime_gb=1, min_vram_gb=1,
            context_window=8192, n_layers=8, estimated_tps_at={},
        )
        bindings[sid] = [NodeBinding(
            node_id=f"node-{domain}", specialist_id=sid, health="healthy",
            queue_depth=0, node_url=f"http://{domain}.invalid:11434", last_seen=now,
        )]
    return RegistrySnapshot(snapshot_ts=now, catalog=cards, specialists=bindings)


@pytest.fixture
def heads():
    return Mock(spec=["signals_for"], signals_for=Mock(return_value=SimpleNamespace(
        signals=ClassifierSignals(domain="code", difficulty="medium"),
        jailbreak=False, pii=False, cluster_hint=None, classifier_ms=0.0,
    )))


def test_mixed_parts_classify_only_latest_user_text(snapshot, heads):
    body = {"messages": [
        {"role": "system", "content": "system instruction"},
        {"role": "user", "content": "earlier user text"},
        {"role": "user", "content": [
            {"type": "text", "text": "Explain this"},
            {"type": "image_url", "image_url": {"url": "https://image.invalid/a.png"}},
            {"type": "text", "text": "Python snippet"},
        ]},
        {"role": "assistant", "content": "later assistant text"},
    ]}

    assert prompt_text(body) == "Explain this\nPython snippet"
    result = AutoRouter(heads).select(body, snapshot)

    heads.signals_for.assert_called_once_with("Explain this\nPython snippet")
    assert result.specialist_id == "synthetic-code"
    assert result.node_url == "http://code.invalid:11434"


def test_latest_image_only_user_does_not_reuse_earlier_text(snapshot, heads):
    body = {"messages": [
        {"role": "user", "content": "earlier code question"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://image.invalid/b.png"}},
        ]},
        {"role": "assistant", "content": "later assistant text"},
    ]}

    assert prompt_text(body) == ""
    result = AutoRouter(heads).select(body, snapshot)

    heads.signals_for.assert_not_called()
    assert result.specialist_id == "synthetic-general"
    assert result.node_url == "http://general.invalid:11434"
