"""Keep optional runtime examples aligned with the trusted protocol registry."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from mesh.models import SpecialistCard
from mesh.protocols import JSON_PROTOCOLS, MULTIPART_PROTOCOLS, VIDEO_JOB_PROTOCOL


EXAMPLES = Path(__file__).parents[2] / "examples" / "multimodal"


def _known_capabilities() -> set[str]:
    protocols = (*JSON_PROTOCOLS, *MULTIPART_PROTOCOLS, VIDEO_JOB_PROTOCOL)
    return {protocol.capability for protocol in protocols}


@pytest.mark.parametrize("runtime", ["localai", "vllm-omni"])
def test_runtime_protocol_map_and_card_use_only_trusted_capabilities(runtime: str) -> None:
    runtime_dir = EXAMPLES / runtime
    mapping = json.loads((runtime_dir / "protocols.json").read_text())
    mapped_capabilities = {
        route["capability"] for route in mapping["protocols"]
    }

    assert mapped_capabilities
    assert mapped_capabilities <= _known_capabilities()

    card = SpecialistCard.model_validate(
        tomllib.loads((runtime_dir / "specialist.toml").read_text())
    )
    assert card.required_backend == "external"
    assert set(card.capabilities) <= mapped_capabilities
    assert card.static_base_url in {
        "http://127.0.0.1:8090",
        "http://127.0.0.1:8091",
    }
