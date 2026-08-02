"""Keep optional runtime examples aligned with the trusted protocol registry."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from mesh.models import SpecialistCard
from mesh.protocols import JSON_PROTOCOLS, MULTIPART_PROTOCOLS, VIDEO_JOB_PROTOCOL


EXAMPLES = Path(__file__).parents[2] / "examples" / "multimodal"


def _tuple(protocol) -> tuple[str, str, str]:
    return protocol.method, protocol.public_path, protocol.capability


PROTOCOLS = {
    protocol.protocol_id: protocol
    for protocol in (*JSON_PROTOCOLS, *MULTIPART_PROTOCOLS, VIDEO_JOB_PROTOCOL)
}
VIDEO_CAPABILITY = VIDEO_JOB_PROTOCOL.capability
VIDEO_PATH = VIDEO_JOB_PROTOCOL.public_path
VIDEO_LIFECYCLE = {
    _tuple(VIDEO_JOB_PROTOCOL),
    ("GET", f"{VIDEO_PATH}/{{id}}", VIDEO_CAPABILITY),
    ("GET", f"{VIDEO_PATH}/{{id}}/content", VIDEO_CAPABILITY),
    ("DELETE", f"{VIDEO_PATH}/{{id}}", VIDEO_CAPABILITY),
}
EXPECTED = {
    "localai": {
        "runtime": "LocalAI",
        "version": "4.7.1",
        "card_origin": "http://127.0.0.1:8090",
        "card_capabilities": {
            PROTOCOLS["openai.audio.transcriptions.v1"].capability
        },
        "routes": {
            _tuple(PROTOCOLS["openai.images.generations.v1"]),
            _tuple(PROTOCOLS["openai.images.edits.v1"]),
            _tuple(PROTOCOLS["openai.audio.speech.v1"]),
            _tuple(PROTOCOLS["openai.audio.transcriptions.v1"]),
            _tuple(PROTOCOLS["localai.video.v1"]),
        },
        "pin": "localai/localai:v4.7.1",
    },
    "vllm-omni": {
        "runtime": "vLLM-Omni",
        "version": "0.20.0",
        "card_origin": "http://127.0.0.1:8091",
        "card_capabilities": {VIDEO_CAPABILITY},
        "routes": {
            _tuple(PROTOCOLS["openai.images.generations.v1"]),
            _tuple(PROTOCOLS["openai.images.edits.v1"]),
            _tuple(PROTOCOLS["openai.audio.speech.v1"]),
            _tuple(PROTOCOLS["vllm_omni.audio.generate.v1"]),
            *VIDEO_LIFECYCLE,
        },
        "pin": "vllm-omni==0.20.0",
    },
}


@pytest.mark.parametrize("runtime", ["localai", "vllm-omni"])
def test_runtime_protocol_map_and_card_use_only_trusted_capabilities(runtime: str) -> None:
    runtime_dir = EXAMPLES / runtime
    expected = EXPECTED[runtime]
    mapping = json.loads((runtime_dir / "protocols.json").read_text())
    routes = mapping["protocols"]
    mapped_routes = [
        (route["method"], route["path"], route["capability"])
        for route in routes
    ]

    assert set(mapping) == {"runtime", "runtime_version", "protocols"}
    assert mapping["runtime"] == expected["runtime"]
    assert mapping["runtime_version"] == expected["version"]
    assert all(set(route) == {"method", "path", "capability"} for route in routes)
    assert len(mapped_routes) == len(set(mapped_routes)), "duplicate protocol mapping"
    assert set(mapped_routes) == expected["routes"]

    card_text = (runtime_dir / "specialist.toml").read_text()
    card = SpecialistCard.model_validate(
        tomllib.loads(card_text)
    )
    assert card.required_backend == "external"
    assert card.static_base_url == expected["card_origin"]
    assert set(card.capabilities) == expected["card_capabilities"]
    assert f"{expected['runtime']} {expected['version']}" in card_text

    runtime_config = (
        (runtime_dir / "docker-compose.yml").read_text()
        if runtime == "localai"
        else (EXAMPLES / "README.md").read_text()
    )
    assert expected["pin"] in runtime_config
