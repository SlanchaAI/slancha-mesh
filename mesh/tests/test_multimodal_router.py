"""Typed JSON media routing through capability-advertising mesh nodes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mesh.models import NodeBinding, NodeSummary, RegistrySnapshot, SpecialistCard
from mesh.protocols import JSON_PROTOCOLS_BY_PATH
from mesh.router_app import create_router_app


SPECIALIST_ID = "media-specialist"
NODE_URL = "http://media-node:8091"


def _card(
    *,
    capabilities: list[str],
    required_backend: str = "external",
    served_model_name: str | None = "upstream-media-model",
) -> SpecialistCard:
    return SpecialistCard(
        model_id="example/media-model",
        specialist_id=SPECIALIST_ID,
        domain="general",
        difficulty_tiers=["medium"],
        required_backend=required_backend,  # type: ignore[arg-type]
        served_model_name=served_model_name,
        storage_gb=1.0,
        runtime_gb=1.0,
        min_vram_gb=1.0,
        context_window=4096,
        n_layers=1,
        estimated_tps_at={"test": 1.0},
        capabilities=capabilities,
    )


def _binding(
    *,
    node_id: str = "media-node",
    node_url: str | None = NODE_URL,
    health: str = "healthy",
) -> NodeBinding:
    return NodeBinding(
        node_id=node_id,
        specialist_id=SPECIALIST_ID,
        health=health,  # type: ignore[arg-type]
        queue_depth=0,
        p95_latency_ms_60s=25.0,
        node_url=node_url,
        last_seen=datetime.now(timezone.utc),
    )


def _snapshot(
    *,
    capabilities: list[str],
    bindings: list[NodeBinding] | None = None,
    required_backend: str = "external",
    served_model_name: str | None = "upstream-media-model",
) -> RegistrySnapshot:
    card = _card(
        capabilities=capabilities,
        required_backend=required_backend,
        served_model_name=served_model_name,
    )
    selected_bindings = bindings if bindings is not None else [_binding()]
    now = datetime.now(timezone.utc)
    return RegistrySnapshot(
        snapshot_ts=now,
        nodes={
            binding.node_id: NodeSummary(
                node_id=binding.node_id,
                friendly_name=binding.node_id,
                health=binding.health,
                last_seen=now,
                loaded_specialist_ids=[SPECIALIST_ID],
                queue_depth=binding.queue_depth,
                p95_latency_ms_60s=binding.p95_latency_ms_60s,
                node_url=binding.node_url,
            )
            for binding in selected_bindings
        },
        specialists={SPECIALIST_ID: selected_bindings},
        coverage={},
        ranked_routes={},
        catalog={SPECIALIST_ID: card},
    )


def _client(snapshot: RegistrySnapshot, handler) -> TestClient:
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(
        create_router_app(snapshot_source=lambda: snapshot, http_client=upstream)
    )


@pytest.mark.parametrize(
    (
        "public_path",
        "protocol_id",
        "upstream_path",
        "request_body",
        "response_body",
        "response_media_type",
    ),
    [
        (
            "/v1/images/generations",
            "openai.images.generations.v1",
            "/v1/images/generations",
            {"prompt": "a red cube"},
            json.dumps({"created": 1, "data": [{"b64_json": "aW1hZ2U="}]}).encode(),
            "application/json",
        ),
        (
            "/v1/audio/speech",
            "openai.audio.speech.v1",
            "/v1/audio/speech",
            {"input": "hello", "voice": "alloy"},
            b"RIFF-speech",
            "audio/wav",
        ),
        (
            "/v1/audio/generate",
            "vllm_omni.audio.generate.v1",
            "/v1/audio/generate",
            {"input": "ocean waves"},
            b"RIFF-sound",
            "audio/wav",
        ),
        (
            "/video",
            "localai.video.v1",
            "/video",
            {"prompt": "a red cube rotates", "response_format": "b64_json"},
            json.dumps({"created": 1, "data": [{"b64_json": "dmlkZW8="}]}).encode(),
            "application/json",
        ),
    ],
)
def test_each_json_media_protocol_routes_only_to_its_trusted_upstream_path(
    public_path: str,
    protocol_id: str,
    upstream_path: str,
    request_body: dict[str, Any],
    response_body: bytes,
    response_media_type: str,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            content=response_body,
            headers={"content-type": response_media_type},
        )

    snapshot = _snapshot(capabilities=[f"protocol:{protocol_id}"])
    response = _client(snapshot, handler).post(
        public_path,
        json={"model": SPECIALIST_ID, **request_body},
    )

    assert response.status_code == 200, response.text
    assert response.content == response_body
    assert captured["method"] == "POST"
    assert captured["path"] == upstream_path
    assert captured["body"]["model"] == "upstream-media-model"
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "media-node"
    assert f"protocol={protocol_id}" in response.headers["X-Slancha-Reason"]


def test_media_route_rejects_missing_explicit_model_before_upstream() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    protocol_id = "openai.images.generations.v1"
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]), handler
    ).post("/v1/images/generations", json={"prompt": "missing model"})

    assert response.status_code == 400
    assert "model" in response.json()["detail"]
    assert calls == 0


def test_media_route_punts_when_explicit_model_lacks_protocol_capability() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    response = _client(_snapshot(capabilities=["vision"]), handler).post(
        "/v1/images/generations",
        json={"model": SPECIALIST_ID, "prompt": "red cube"},
    )

    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert response.json()["error"]["type"] == "slancha_punt"
    assert response.json()["error"]["code"] == "no_suitable_local_route"
    assert response.json()["error"]["details"]["local_attempts"] == 0
    assert calls == 0


def test_media_route_rewrites_alias_and_replaces_caller_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["model"] = json.loads(request.read())["model"]
        return httpx.Response(
            200,
            content=b"RIFF",
            headers={"content-type": "audio/wav"},
        )

    monkeypatch.setenv("SLANCHA_UPSTREAM_TOKEN", "node-only-secret")
    protocol_id = "openai.audio.speech.v1"
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]), handler
    ).post(
        "/v1/audio/speech",
        json={"model": SPECIALIST_ID, "input": "hello", "voice": "alloy"},
        headers={"Authorization": "Bearer caller-secret"},
    )

    assert response.status_code == 200
    assert captured == {
        "authorization": "Bearer node-only-secret",
        "model": "upstream-media-model",
    }


def test_media_route_strips_caller_authorization_without_upstream_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_authorization: str | None = "not-called"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_authorization
        captured_authorization = request.headers.get("authorization")
        return httpx.Response(
            200,
            content=b"RIFF",
            headers={"content-type": "audio/wav"},
        )

    monkeypatch.delenv("SLANCHA_UPSTREAM_TOKEN", raising=False)
    protocol_id = "openai.audio.speech.v1"
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]), handler
    ).post(
        "/v1/audio/speech",
        json={"model": SPECIALIST_ID, "input": "hello", "voice": "alloy"},
        headers={"Authorization": "Bearer caller-secret"},
    )

    assert response.status_code == 200
    assert captured_authorization is None


def test_media_route_rejects_non_json_request_encoding() -> None:
    protocol_id = "openai.images.generations.v1"
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]),
        lambda request: pytest.fail("non-JSON request reached upstream"),
    ).post(
        "/v1/images/generations",
        content=b"model=media-specialist&prompt=red+cube",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 415


def test_media_route_rejects_declared_request_over_protocol_body_limit() -> None:
    protocol = JSON_PROTOCOLS_BY_PATH["/v1/images/generations"]
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol.protocol_id}"]),
        lambda request: pytest.fail("oversized request reached upstream"),
    ).post(
        protocol.public_path,
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(protocol.max_request_bytes + 1),
        },
    )

    assert response.status_code == 413


def test_media_route_rejects_actual_request_over_limit_when_length_header_lies() -> None:
    protocol = JSON_PROTOCOLS_BY_PATH["/v1/images/generations"]
    oversized = json.dumps(
        {
            "model": SPECIALIST_ID,
            "prompt": "x" * protocol.max_request_bytes,
        }
    ).encode()
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol.protocol_id}"]),
        lambda request: pytest.fail("oversized request reached upstream"),
    ).post(
        protocol.public_path,
        content=oversized,
        headers={"Content-Type": "application/json", "Content-Length": "2"},
    )

    assert len(oversized) > protocol.max_request_bytes
    assert response.status_code == 413


def test_media_route_rejects_actual_upstream_response_over_protocol_limit() -> None:
    protocol = JSON_PROTOCOLS_BY_PATH["/v1/images/generations"]
    oversized = b"x" * (protocol.max_response_bytes + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=oversized,
            headers={"content-type": "application/json"},
        )

    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol.protocol_id}"]), handler
    ).post(
        protocol.public_path,
        json={"model": SPECIALIST_ID, "prompt": "red cube"},
    )

    assert response.status_code == 502
    assert response.headers.get("X-Slancha-Outcome") is None
    assert "response body exceeds" in response.json()["detail"]


def test_media_route_rejects_unexpected_success_media_type() -> None:
    protocol_id = "openai.images.generations.v1"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not an image response</html>",
            headers={"content-type": "text/html"},
        )

    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]), handler
    ).post(
        "/v1/images/generations",
        json={"model": SPECIALIST_ID, "prompt": "red cube"},
    )

    assert response.status_code == 502
    assert response.headers.get("X-Slancha-Outcome") is None
    assert "unexpected content type" in response.json()["detail"]


def test_media_route_attempts_only_one_node_after_upstream_connect_failure() -> None:
    attempted_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted_hosts.append(request.url.host)
        raise httpx.ConnectError("connection lost after send", request=request)

    protocol_id = "vllm_omni.audio.generate.v1"
    snapshot = _snapshot(
        capabilities=[f"protocol:{protocol_id}"],
        bindings=[
            _binding(node_id="first", node_url="http://first:8091"),
            _binding(node_id="second", node_url="http://second:8091"),
        ],
    )
    response = _client(snapshot, handler).post(
        "/v1/audio/generate",
        json={"model": SPECIALIST_ID, "input": "waves"},
    )

    assert response.status_code == 502
    assert response.headers.get("X-Slancha-Outcome") is None
    assert attempted_hosts == ["first"]


def test_media_route_returns_typed_punt_when_capable_node_is_unreachable() -> None:
    protocol_id = "openai.audio.speech.v1"
    response = _client(
        _snapshot(
            capabilities=[f"protocol:{protocol_id}"],
            bindings=[_binding(health="unreachable")],
        ),
        lambda request: pytest.fail("unreachable node received request"),
    ).post(
        "/v1/audio/speech",
        json={"model": SPECIALIST_ID, "input": "hello", "voice": "alloy"},
    )

    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert response.json()["error"]["code"] == "local_route_unavailable"
    assert response.json()["error"]["details"]["retryable"] is True


def test_localai_video_requires_inline_base64_response_format() -> None:
    protocol_id = "localai.video.v1"
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]),
        lambda request: pytest.fail("unsafe URL response mode reached upstream"),
    ).post(
        "/video",
        json={
            "model": SPECIALIST_ID,
            "prompt": "red cube rotates",
            "response_format": "url",
        },
    )

    assert response.status_code == 400
    assert "b64_json" in response.json()["detail"]
