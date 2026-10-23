"""Exact hard-filter boundaries through the public, pure allocator."""

import math

import pytest

from mesh.allocator import allocate_cluster
from mesh.models import SpecialistCard


@pytest.mark.parametrize("resource", ["disk_free_gb", "ram_available_gb"])
def test_allocate_accepts_exact_resource_threshold(resource, spark_node):
    card = SpecialistCard(
        model_id="synthetic/boundary",
        specialist_id="boundary",
        domain="general",
        difficulty_tiers=["easy"],
        required_backend="vllm",
        storage_gb=4.0,
        runtime_gb=8.0,
        min_vram_gb=8.0,
        context_window=4096,
        n_layers=1,
    )
    # Unified memory reserves 8 GB for the OS: 16 GB available gives
    # exactly the card's 8 GB minimum. Keep the other resource plentiful.
    threshold = 4.0 if resource == "disk_free_gb" else 16.0
    at_limit = spark_node.model_copy(update={resource: threshold})
    below_limit = at_limit.model_copy(update={resource: threshold - 0.25})

    accepted = allocate_cluster(
        [at_limit], [card], strategy="best_per_machine"
    )[at_limit.node_id]
    assert accepted.primary == card
    assert math.isfinite(accepted.fit_score)

    rejected = allocate_cluster(
        [below_limit], [card], strategy="best_per_machine"
    )[below_limit.node_id]
    assert rejected.primary is None
    assert rejected.fit_score == -math.inf
    assert rejected.alternates == []
