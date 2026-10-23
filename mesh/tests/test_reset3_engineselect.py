"""Synthetic boundary and independence checks for engine recommendations."""

from dataclasses import replace
from math import inf, nextafter

import pytest

from mesh.engine_select import recommend_engine
from mesh.models import NodeProbe


@pytest.fixture
def discrete_node():
    return NodeProbe(
        node_id="synthetic", friendly_name="synthetic", chip="generic",
        arch="x86_64", cuda_capability="8.9", unified_memory=False,
        ram_total_gb=64.0, ram_available_gb=48.0,
        vram_total_gb=32.0, vram_available_gb=24.0,
        available_backends=[],
    )


@pytest.mark.parametrize("vram,backend,quant,alternates", [
    (nextafter(24.0, -inf), "ollama", "gguf-q4", ("llamacpp", "vllm")),
    (24.0, "vllm", "fp8", ("ollama", "llamacpp")),
    (nextafter(24.0, inf), "vllm", "fp8", ("ollama", "llamacpp")),
])
def test_vram_threshold(discrete_node, vram, backend, quant, alternates):
    node = discrete_node.model_copy(update={"vram_available_gb": vram})
    rec = recommend_engine(node, os_name="Linux")
    assert (rec.backend, rec.quant, rec.alternates) == (backend, quant, alternates)
    assert rec.installed is False


def test_unknown_compute_capability_uses_awq(discrete_node):
    node = discrete_node.model_copy(update={"cuda_capability": "unknown"})
    rec = recommend_engine(node, os_name="Linux")
    assert (rec.backend, rec.quant) == ("vllm", "awq")
    assert rec.alternates == ("ollama", "llamacpp")


@pytest.mark.parametrize("vram,backend", [(23.0, "ollama"), (24.0, "vllm")])
def test_installed_backends_only_change_installed_flag(discrete_node, vram, backend):
    node = discrete_node.model_copy(update={"vram_available_gb": vram})
    baseline = recommend_engine(node, os_name="Linux")
    assert baseline.backend == backend
    assert baseline.installed is False
    for available, installed in [
        (["llamacpp"], False),
        ([backend], True),
        (["llamacpp", "vllm", "ollama"], True),
    ]:
        rec = recommend_engine(
            node.model_copy(update={"available_backends": available}), os_name="Linux",
        )
        assert rec == replace(baseline, installed=installed)
