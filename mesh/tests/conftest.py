"""Shared pytest fixtures for the mesh test suite.

Synthetic NodeProbes here are NOT real Sparks — they're deterministic
mocks parameterized to exercise the allocator's hard/soft filter paths.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mesh.catalog import load_catalog
from mesh.models import (
    LoadedModel,
    NodeHeartbeat,
    NodeProbe,
    NodeUtilization,
    SpecialistCard,
)


@pytest.fixture
def catalog() -> list[SpecialistCard]:
    return load_catalog()


@pytest.fixture
def spark_node() -> NodeProbe:
    """Synthetic Spark GB10 — unified mem, no measured bandwidth, vLLM."""
    return NodeProbe(
        node_id="spark-1",
        friendly_name="spark-1",
        chip="NVIDIA GB10",
        arch="aarch64",
        cuda_capability="12.1",
        fp4_tops=3800.0,
        fp16_tops=250.0,
        ram_total_gb=128.0,
        ram_available_gb=110.0,
        vram_total_gb=None,
        vram_available_gb=None,
        unified_memory=True,
        memory_bandwidth_gbs=273.0,
        available_backends=["vllm", "llamacpp"],
        disk_free_gb=500.0,
        rtt_to_master_ms=2.0,
    )


@pytest.fixture
def mac_mini_node() -> NodeProbe:
    """Synthetic Mac mini M4 — unified mem, llamacpp/mlx only, smaller RAM."""
    return NodeProbe(
        node_id="mac-mini-1",
        friendly_name="mac-mini-1",
        chip="Apple M4 Max",
        arch="apple-silicon",
        cuda_capability=None,
        fp4_tops=None,
        fp16_tops=36.0,
        ram_total_gb=64.0,
        ram_available_gb=50.0,
        vram_total_gb=None,
        vram_available_gb=None,
        unified_memory=True,
        memory_bandwidth_gbs=546.0,
        available_backends=["llamacpp", "mlx"],
        disk_free_gb=300.0,
        rtt_to_master_ms=5.0,
    )


@pytest.fixture
def tiny_node() -> NodeProbe:
    """Underpowered node — should fail hard filters on most specialists."""
    return NodeProbe(
        node_id="rpi-1",
        friendly_name="rpi-1",
        chip="Broadcom BCM2712",
        arch="aarch64",
        cuda_capability=None,
        ram_total_gb=8.0,
        ram_available_gb=3.0,
        unified_memory=True,
        memory_bandwidth_gbs=20.0,
        available_backends=["llamacpp"],
        disk_free_gb=20.0,
        rtt_to_master_ms=10.0,
    )


@pytest.fixture
def fresh_now() -> datetime:
    return datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)


def make_heartbeat(
    node: NodeProbe,
    ts: datetime,
    loaded_specialist_ids: list[str],
    catalog: list[SpecialistCard],
    queue_depth: int = 0,
    p95_ms: float | None = 800.0,
    health: str = "healthy",
) -> NodeHeartbeat:
    by_id = {c.specialist_id: c for c in catalog}
    return NodeHeartbeat(
        node_id=node.node_id,
        ts=ts,
        hardware=node,
        loaded_models=[
            LoadedModel(
                specialist_id=sid,
                model_id=by_id[sid].model_id,
                loaded_at=ts,
                estimated_tps=60.0,
            )
            for sid in loaded_specialist_ids
        ],
        util=NodeUtilization(
            gpu_util_pct=20.0,
            ram_util_pct=40.0,
            queue_depth=queue_depth,
            p50_latency_ms_60s=p95_ms / 2 if p95_ms else None,
            p95_latency_ms_60s=p95_ms,
        ),
        health=health,  # type: ignore[arg-type]
    )
