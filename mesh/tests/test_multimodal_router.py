"""Typed JSON media routing through capability-advertising mesh nodes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mesh.models import NodeBinding, NodeSummary, RegistrySnapshot, SpecialistCard
from mesh.protocols import JSON_PROTOCOLS_BY_PATH, MULTIPART_PROTOCOLS_BY_PATH
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


def _multipart_parts(request: httpx.Request) -> list[dict[str, Any]]:
    """Decode an outbound multipart request with the stdlib MIME parser."""

    content_type = request.headers["content-type"]
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request.read()
    )
    return [
        {
            "name": part.get_param("name", header="content-disposition"),
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
            "content": part.get_payload(decode=True),
        }
        for part in message.iter_parts()
    ]


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


async def test_media_route_stops_streaming_request_when_length_header_lies() -> None:
    protocol = JSON_PROTOCOLS_BY_PATH["/v1/images/generations"]

    class ManyChunks(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.yielded = 0

        async def __aiter__(self):
            for _ in range(20):
                self.yielded += 1
                yield b"x" * (protocol.max_request_bytes // 4)

    stream = ManyChunks()
    snapshot = _snapshot(capabilities=[f"protocol:{protocol.protocol_id}"])
    app = create_router_app(
        snapshot_source=lambda: snapshot,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("oversized request reached upstream")
            )
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://router",
    ) as client:
        response = await client.post(
            protocol.public_path,
            content=stream,
            headers={"Content-Type": "application/json", "Content-Length": "2"},
        )

    assert response.status_code == 413
    assert stream.yielded == 5


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
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "media-node"
    assert "response_too_large" in response.headers["X-Slancha-Reason"]


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
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "media-node"
    assert "unexpected_media_type" in response.headers["X-Slancha-Reason"]


def test_media_route_disables_redirects_on_injected_following_client() -> None:
    protocol_id = "openai.images.generations.v1"
    attempted_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted_urls.append(str(request.url))
        if request.url.host == "media-node":
            return httpx.Response(
                307,
                headers={"location": "http://attacker.invalid/capture"},
            )
        return httpx.Response(
            200,
            json={"data": [{"b64_json": "c3RvbGVu"}]},
            headers={"content-type": "application/json"},
        )

    snapshot = _snapshot(capabilities=[f"protocol:{protocol_id}"])
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    app = create_router_app(
        snapshot_source=lambda: snapshot,
        http_client=upstream,
    )
    response = TestClient(app).post(
        "/v1/images/generations",
        json={"model": SPECIALIST_ID, "prompt": "red cube"},
    )

    assert response.status_code == 502
    assert attempted_urls == [f"{NODE_URL}/v1/images/generations"]
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "media-node"
    assert "redirect_rejected" in response.headers["X-Slancha-Reason"]


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
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "first"
    assert "transport_failure" in response.headers["X-Slancha-Reason"]


def test_media_route_rejects_node_url_path_and_query_before_upstream() -> None:
    protocol_id = "openai.images.generations.v1"
    response = _client(
        _snapshot(
            capabilities=[f"protocol:{protocol_id}"],
            bindings=[
                _binding(
                    node_url=(
                        "http://media-node:8091/attacker-prefix"
                        "?redirect=/v1/images/generations"
                    )
                )
            ],
        ),
        lambda request: pytest.fail("path-bearing node URL reached upstream"),
    ).post(
        "/v1/images/generations",
        json={"model": SPECIALIST_ID, "prompt": "red cube"},
    )

    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert response.json()["error"]["details"]["local_attempts"] == 0


def test_media_route_skips_invalid_node_origin_and_uses_later_valid_binding() -> None:
    protocol_id = "openai.images.generations.v1"
    attempted_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={"data": [{"b64_json": "aW1hZ2U="}]},
            headers={"content-type": "application/json"},
        )

    response = _client(
        _snapshot(
            capabilities=[f"protocol:{protocol_id}"],
            bindings=[
                _binding(node_id="bad", node_url="http://bad:8091/prefix"),
                _binding(node_id="good", node_url="http://good:8091/"),
            ],
        ),
        handler,
    ).post(
        "/v1/images/generations",
        json={"model": SPECIALIST_ID, "prompt": "red cube"},
    )

    assert response.status_code == 200
    assert attempted_urls == ["http://good:8091/v1/images/generations"]
    assert response.headers["X-Slancha-Node"] == "good"


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


@pytest.mark.parametrize(
    "upstream_payload",
    [
        {"data": [{"url": "/generated/video.mp4"}]},
        {"data": [{}]},
        {"data": [{"b64_json": "", "url": "/generated/video.mp4"}]},
        {"data": []},
    ],
)
def test_localai_video_rejects_success_without_only_inline_b64_data(
    upstream_payload: dict[str, Any],
) -> None:
    protocol_id = "localai.video.v1"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=upstream_payload,
            headers={"content-type": "application/json"},
        )

    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]), handler
    ).post(
        "/video",
        json={
            "model": SPECIALIST_ID,
            "prompt": "red cube rotates",
            "response_format": "b64_json",
        },
    )

    assert response.status_code == 502
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "media-node"
    assert "invalid_inline_video" in response.headers["X-Slancha-Reason"]


@pytest.mark.parametrize(
    ("public_path", "protocol_id", "files", "upstream_content_type", "upstream_body"),
    [
        (
            "/v1/images/edits",
            "openai.images.edits.v1",
            [
                ("model", (None, SPECIALIST_ID)),
                ("prompt", (None, "first prompt")),
                ("prompt", (None, "second prompt")),
                ("image", ("input.png", b"\x89PNG\r\nmedia", "image/png")),
            ],
            "application/json",
            b'{"data":[{"b64_json":"aW1hZ2U="}]}',
        ),
        (
            "/v1/audio/transcriptions",
            "openai.audio.transcriptions.v1",
            [
                ("model", (None, SPECIALIST_ID)),
                ("response_format", (None, "text")),
                ("timestamp_granularities[]", (None, "word")),
                ("timestamp_granularities[]", (None, "segment")),
                ("file", ("sample.wav", b"RIFFaudio", "audio/wav")),
            ],
            "text/plain; charset=utf-8",
            b"hello from the mesh",
        ),
    ],
)
def test_each_multipart_protocol_preserves_safe_parts_and_rewrites_model(
    monkeypatch: pytest.MonkeyPatch,
    public_path: str,
    protocol_id: str,
    files: list[tuple[str, tuple[str | None, str | bytes] | tuple[str, bytes, str]]],
    upstream_content_type: str,
    upstream_body: bytes,
) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["authorization"] = request.headers.get("authorization")
        captured["cookie"] = request.headers.get("cookie")
        captured["content_type"] = request.headers["content-type"]
        captured["parts"] = _multipart_parts(request)
        return httpx.Response(
            200,
            content=upstream_body,
            headers={"content-type": upstream_content_type},
        )

    monkeypatch.setenv("SLANCHA_UPSTREAM_TOKEN", "node-only-secret")
    response = _client(
        _snapshot(capabilities=[f"protocol:{protocol_id}"]), handler
    ).post(
        public_path,
        files=files,
        headers={
            "Authorization": "Bearer caller-secret",
            "Cookie": "session=caller-secret",
        },
    )

    assert response.status_code == 200, response.text
    assert response.content == upstream_body
    assert captured["method"] == "POST"
    assert captured["path"] == public_path
    assert captured["authorization"] == "Bearer node-only-secret"
    assert captured["cookie"] is None
    assert "multipart/form-data; boundary=" in captured["content_type"]
    parts = captured["parts"]
    assert [p["content"] for p in parts if p["name"] == "model"] == [
        b"upstream-media-model"
    ]
    if public_path.endswith("/edits"):
        assert [p["content"] for p in parts if p["name"] == "prompt"] == [
            b"first prompt",
            b"second prompt",
        ]
        image = next(p for p in parts if p["name"] == "image")
        assert image == {
            "name": "image",
            "filename": "input.png",
            "content_type": "image/png",
            "content": b"\x89PNG\r\nmedia",
        }
    else:
        assert [
            p["content"] for p in parts if p["name"] == "timestamp_granularities[]"
        ] == [b"word", b"segment"]
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "media-node"
    assert f"protocol={protocol_id}" in response.headers["X-Slancha-Reason"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("stream", "true"), ("partial_images", "2")],
)
def test_image_edit_rejects_streaming_controls_before_upstream(
    field: str,
    value: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"data": [{"b64_json": "aW1hZ2U="}]},
            headers={"content-type": "application/json"},
        )

    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        handler,
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            (field, (None, value)),
            ("image", ("input.png", b"image", "image/png")),
        ],
    )

    assert response.status_code == 400
    assert "unsupported multipart field" in response.json()["detail"]
    assert calls == 0


@pytest.mark.parametrize("model_parts", [[], [SPECIALIST_ID, SPECIALIST_ID]])
def test_multipart_route_requires_exactly_one_model_before_upstream(
    model_parts: list[str],
) -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    files = [("model", (None, value)) for value in model_parts]
    files.append(("image", ("input.png", b"image", "image/png")))
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("invalid model shape reached upstream"),
    ).post(protocol.public_path, files=files)

    assert response.status_code == 400
    assert "exactly one" in response.json()["detail"]


def test_multipart_route_punts_when_specialist_lacks_protocol_capability() -> None:
    response = _client(
        _snapshot(capabilities=["vision"]),
        lambda request: pytest.fail("capability mismatch reached upstream"),
    ).post(
        "/v1/images/edits",
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("image", ("input.png", b"image", "image/png")),
        ],
    )

    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert response.json()["error"]["code"] == "no_suitable_local_route"


def test_multipart_route_rejects_malformed_body_before_upstream() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("malformed multipart reached upstream"),
    ).post(
        protocol.public_path,
        content=b"not-a-valid-multipart-body",
        headers={"Content-Type": "multipart/form-data; boundary=broken"},
    )

    assert response.status_code == 400
    assert "multipart" in response.json()["detail"].lower()


def test_multipart_route_rejects_declared_aggregate_over_limit() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("oversized request reached upstream"),
    ).post(
        protocol.public_path,
        content=b"x",
        headers={
            "Content-Type": "multipart/form-data; boundary=x",
            "Content-Length": str(protocol.max_request_bytes + 1),
        },
    )

    assert response.status_code == 413


def test_multipart_route_rejects_field_over_limit() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("oversized field reached upstream"),
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("prompt", (None, "x" * (protocol.max_field_bytes + 1))),
            ("image", ("input.png", b"image", "image/png")),
        ],
    )

    assert response.status_code == 413


def test_multipart_route_rejects_file_over_limit() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/audio/transcriptions"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("oversized file reached upstream"),
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            (
                "file",
                ("sample.wav", b"x" * (protocol.max_file_bytes + 1), "audio/wav"),
            ),
        ],
    )

    assert response.status_code == 413


@pytest.mark.parametrize(
    "parts",
    [
        [("extra", (None, str(i))) for i in range(40)],
        [("image", (f"{i}.png", b"", "image/png")) for i in range(20)],
    ],
)
def test_multipart_route_rejects_field_or_file_count_over_limit(
    parts: list[tuple[str, tuple[str | None, str] | tuple[str, bytes, str]]],
) -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("excess multipart parts reached upstream"),
    ).post(
        protocol.public_path,
        files=[("model", (None, SPECIALIST_ID)), *parts],
    )

    assert response.status_code in {400, 413}


def test_multipart_route_rejects_unsafe_filename_before_upstream() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: pytest.fail("unsafe filename reached upstream"),
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("image", ("../input.png", b"image", "image/png")),
        ],
    )

    assert response.status_code == 400
    assert "filename" in response.json()["detail"]


def test_transcription_response_type_must_match_requested_output_mode() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/audio/transcriptions"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: httpx.Response(
            200,
            content=b'{"text":"wrong wire type"}',
            headers={"content-type": "application/json"},
        ),
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("response_format", (None, "text")),
            ("file", ("sample.wav", b"RIFF", "audio/wav")),
        ],
    )

    assert response.status_code == 502
    assert "unexpected_media_type" in response.headers["X-Slancha-Reason"]


@pytest.mark.parametrize("failure", ["redirect", "transport", "server_error"])
def test_multipart_non_idempotent_request_never_falls_back_after_attempt(
    failure: str,
) -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    attempted_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted_hosts.append(request.url.host)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "http://attacker.invalid"})
        if failure == "transport":
            raise httpx.ConnectError("lost after send", request=request)
        return httpx.Response(
            503,
            content=b"unavailable",
            headers={"content-type": "text/plain"},
        )

    response = _client(
        _snapshot(
            capabilities=[protocol.capability],
            bindings=[
                _binding(node_id="first", node_url="http://first:8091"),
                _binding(node_id="second", node_url="http://second:8091"),
            ],
        ),
        handler,
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("image", ("input.png", b"image", "image/png")),
        ],
    )

    assert attempted_hosts == ["first"]
    assert response.status_code in {502, 503}
    assert response.headers["X-Slancha-Node"] == "first"


def test_multipart_route_rejects_oversized_upstream_response() -> None:
    protocol = MULTIPART_PROTOCOLS_BY_PATH["/v1/images/edits"]
    response = _client(
        _snapshot(capabilities=[protocol.capability]),
        lambda request: httpx.Response(
            200,
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(protocol.max_response_bytes + 1),
            },
        ),
    ).post(
        protocol.public_path,
        files=[
            ("model", (None, SPECIALIST_ID)),
            ("image", ("input.png", b"image", "image/png")),
        ],
    )

    assert response.status_code == 502
    assert "response_too_large" in response.headers["X-Slancha-Reason"]


VIDEO_JOB_CAPABILITY = "protocol:vllm_omni.video.jobs.v1"


def _video_app(snapshot: RegistrySnapshot, handler, db_path):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return create_router_app(
        snapshot_source=lambda: snapshot,
        http_client=upstream,
        video_job_db_path=db_path,
    )


def test_video_job_owner_survives_restart_and_stays_pinned(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "router" / "video-jobs.sqlite3"
    owner = _binding(node_id="owner", node_url="http://owner:8091")
    fallback = _binding(node_id="fallback", node_url="http://fallback:8091")
    calls: list[tuple[str, str, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            (
                request.method,
                request.url.host,
                request.url.path,
                request.headers.get("authorization"),
            )
        )
        assert request.headers.get("cookie") is None
        if request.method == "POST":
            fields = {part["name"]: part for part in _multipart_parts(request)}
            assert fields["model"]["content"] == b"upstream-media-model"
            assert fields["prompt"]["content"] == b"a safe prompt"
            return httpx.Response(
                200,
                json={
                    "id": "video_gen_upstream_123",
                    "object": "video",
                    "status": "queued",
                    "created_at": 1701234567,
                    "prompt": "a safe prompt",
                    "file_name": "/var/tmp/private.mp4",
                    "content_url": "http://owner:8091/private.mp4",
                },
            )
        if request.url.path.endswith("/content"):
            return httpx.Response(
                200,
                content=b"video-bytes",
                headers={"content-type": "video/mp4"},
            )
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={
                    "id": "video_gen_upstream_123",
                    "deleted": True,
                    "object": "video.deleted",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "video_gen_upstream_123",
                "object": "video",
                "status": "completed",
                "progress": 100,
                "created_at": 1701234567,
                "file_name": "/var/tmp/private.mp4",
                "url": "http://owner:8091/private.mp4",
            },
        )

    monkeypatch.setenv("SLANCHA_UPSTREAM_TOKEN", "node-only-secret")
    initial_snapshot = _snapshot(
        capabilities=[VIDEO_JOB_CAPABILITY], bindings=[owner, fallback]
    )
    with TestClient(_video_app(initial_snapshot, handler, db_path)) as client:
        created = client.post(
            "/v1/videos",
            files=[
                ("model", (None, SPECIALIST_ID)),
                ("prompt", (None, "a safe prompt")),
            ],
            headers={
                "Authorization": "Bearer caller-secret",
                "Cookie": "session=caller-cookie",
            },
        )

    assert created.status_code == 200, created.text
    public_id = created.json()["id"]
    assert public_id.startswith("video_")
    assert "video_gen_upstream_123" not in created.text
    assert "private.mp4" not in created.text
    assert "content_url" not in created.json()

    restarted_snapshot = _snapshot(
        capabilities=[VIDEO_JOB_CAPABILITY], bindings=[fallback, owner]
    )
    with TestClient(_video_app(restarted_snapshot, handler, db_path)) as client:
        polled = client.get(f"/v1/videos/{public_id}")
        content = client.get(f"/v1/videos/{public_id}/content")
        deleted = client.delete(f"/v1/videos/{public_id}")
        gone = client.get(f"/v1/videos/{public_id}")

    assert polled.status_code == 200
    assert polled.json()["id"] == public_id
    assert polled.json()["status"] == "completed"
    assert "private.mp4" not in polled.text
    assert content.status_code == 200
    assert content.content == b"video-bytes"
    assert content.headers["content-type"] == "video/mp4"
    assert deleted.status_code == 200
    assert deleted.json() == {
        "id": public_id,
        "deleted": True,
        "object": "video.deleted",
    }
    assert gone.status_code == 404
    assert [(method, host) for method, host, _, _ in calls] == [
        ("POST", "owner"),
        ("GET", "owner"),
        ("GET", "owner"),
        ("DELETE", "owner"),
    ]
    assert all(auth == "Bearer node-only-secret" for *_, auth in calls)


def test_video_create_persistence_failure_reports_bounded_orphan_once(
    tmp_path, caplog
) -> None:
    bad_db_path = tmp_path / "database-is-a-directory"
    bad_db_path.mkdir()
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(
            200,
            json={
                "id": "video_gen_orphan_123",
                "status": "queued",
                "created_at": 1701234567,
            },
        )

    with caplog.at_level("WARNING", logger="mesh.router_app"):
        response = TestClient(
            _video_app(
                _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
                handler,
                bad_db_path,
            )
        ).post(
            "/v1/videos",
            files=[
                ("model", (None, SPECIALIST_ID)),
                ("prompt", (None, "one attempt")),
            ],
        )

    assert upstream_calls == 1
    assert response.status_code == 502
    assert "video_gen_orphan_123" not in response.text
    assert "orphan" in caplog.text.lower()
    assert len(caplog.text) < 2048


def test_video_job_owner_absence_punts_without_reselection(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    owner = _binding(node_id="owner", node_url="http://owner:8091")
    fallback = _binding(node_id="fallback", node_url="http://fallback:8091")
    calls: list[str] = []

    def create_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            200,
            json={"id": "upstream-owner-id", "status": "queued", "created_at": 1},
        )

    with TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY], bindings=[owner, fallback]),
            create_handler,
            db_path,
        )
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
    public_id = created.json()["id"]

    absent_owner_snapshot = _snapshot(
        capabilities=[VIDEO_JOB_CAPABILITY], bindings=[fallback]
    )
    with TestClient(
        _video_app(
            absent_owner_snapshot,
            lambda request: pytest.fail("owner absence touched an upstream node"),
            db_path,
        )
    ) as client:
        response = client.get(f"/v1/videos/{public_id}")

    assert calls == ["owner"]
    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "owner"
    assert response.json()["error"]["details"] == {
        "local_attempts": 0,
        "suggested_class": "cloud",
        "retryable": True,
    }


def test_video_job_ids_are_opaque_and_list_route_is_not_exposed(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
            handler,
            tmp_path / "video-jobs.sqlite3",
        )
    )

    assert client.get("/v1/videos").status_code == 405
    assert client.get("/v1/videos/video_gen_upstream_123").status_code == 404
    assert client.get("/v1/videos/video_../../etc/passwd").status_code == 404
    assert calls == 0


@pytest.mark.parametrize(
    "fields",
    [
        [("prompt", (None, "missing model"))],
        [("model", (None, SPECIALIST_ID))],
    ],
)
def test_video_create_requires_explicit_model_and_prompt(tmp_path, fields) -> None:
    response = TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
            lambda request: pytest.fail("invalid video create reached upstream"),
            tmp_path / "video-jobs.sqlite3",
        )
    ).post("/v1/videos", files=fields)

    assert response.status_code == 400


def test_video_job_rejects_changed_owner_origin_without_touching_it(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    owner = _binding(node_id="owner", node_url="http://owner:8091")

    with TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY], bindings=[owner]),
            lambda request: httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            ),
            db_path,
        )
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )

    changed = _binding(node_id="owner", node_url="http://changed-owner:8091")
    with TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY], bindings=[changed]),
            lambda request: pytest.fail("changed owner origin received a request"),
            db_path,
        )
    ) as client:
        response = client.get(f"/v1/videos/{created.json()['id']}")

    assert response.status_code == 503
    assert response.headers["X-Slancha-Outcome"] == "punt"
    assert "owner_absent" in response.headers["X-Slancha-Reason"]


@pytest.mark.parametrize("failure", ["redirect", "server_error", "oversized"])
def test_video_status_failure_is_bounded_and_never_falls_back(
    tmp_path, failure: str
) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    first = _binding(node_id="first", node_url="http://first:8091")
    second = _binding(node_id="second", node_url="http://second:8091")
    mode = ["create"]
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if mode[0] == "create":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "http://attacker.invalid"})
        if failure == "server_error":
            return httpx.Response(
                503,
                json={"detail": "upstream unavailable"},
            )
        return httpx.Response(
            200,
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": str(1024 * 1024 + 1),
            },
        )

    snapshot = _snapshot(
        capabilities=[VIDEO_JOB_CAPABILITY], bindings=[first, second]
    )
    with TestClient(_video_app(snapshot, handler, db_path)) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        mode[0] = "failure"
        response = client.get(f"/v1/videos/{created.json()['id']}")

    assert hosts == ["first", "first"]
    assert response.status_code in {502, 503}
    assert response.headers["X-Slancha-Node"] == "first"


def test_video_failed_job_status_is_rewritten_and_persisted(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    mode = ["create"]

    def handler(request: httpx.Request) -> httpx.Response:
        if mode[0] == "create":
            return httpx.Response(
                200,
                json={"id": "upstream-failed", "status": "queued", "created_at": 1},
            )
        return httpx.Response(
            422,
            json={
                "id": "upstream-failed",
                "object": "video",
                "status": "failed",
                "created_at": 1,
                "completed_at": 2,
                "error": {"code": 422, "message": "generation rejected"},
                "file_name": "/private/failed.mp4",
            },
        )

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        mode[0] = "failed"
        response = client.get(f"/v1/videos/{created.json()['id']}")

    assert response.status_code == 422
    assert response.json()["id"] == created.json()["id"]
    assert response.json()["status"] == "failed"
    assert response.json()["error"] == {
        "code": "422",
        "message": "generation rejected",
    }
    assert "private" not in response.text
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT last_status FROM video_jobs").fetchone()[0] == "failed"


def test_video_content_rejects_untrusted_media_type(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        return httpx.Response(
            200,
            content=b"<script>bad()</script>",
            headers={"content-type": "text/html"},
        )

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        response = client.get(f"/v1/videos/{created.json()['id']}/content")

    assert response.status_code == 502
    assert "unexpected_media_type" in response.headers["X-Slancha-Reason"]


def test_invalid_upstream_delete_does_not_remove_local_ownership(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    mode = ["create"]

    def handler(request: httpx.Request) -> httpx.Response:
        if mode[0] == "create":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={"id": "wrong-id", "deleted": True, "object": "video.deleted"},
            )
        return httpx.Response(
            200,
            json={"id": "upstream-id", "status": "queued", "created_at": 1},
        )

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        mode[0] = "delete"
        rejected = client.delete(f"/v1/videos/{created.json()['id']}")
        still_owned = client.get(f"/v1/videos/{created.json()['id']}")

    assert rejected.status_code == 502
    assert still_owned.status_code == 200


@pytest.mark.parametrize(
    "upstream_id",
    [".", "..", "%2e%2e", "../escape", "nested/id", r"nested\id"],
)
def test_video_create_rejects_upstream_ids_with_path_semantics(
    tmp_path, upstream_id: str
) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"

    response = TestClient(
        _video_app(
            _snapshot(capabilities=[VIDEO_JOB_CAPABILITY]),
            lambda request: httpx.Response(
                200,
                json={"id": upstream_id, "status": "queued", "created_at": 1},
            ),
            db_path,
        )
    ).post(
        "/v1/videos",
        files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
    )

    assert response.status_code == 502
    assert not db_path.exists()


def test_video_load_rejects_wrong_persisted_protocol_without_owner_call(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"id": "upstream-id", "status": "queued", "created_at": 1},
        )

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE video_jobs SET protocol_id = ?",
                ("some_other_protocol",),
            )
        response = client.get(f"/v1/videos/{created.json()['id']}")

    assert response.status_code == 404
    assert calls == 1


def test_video_status_store_failure_does_not_expose_exception_text(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    mode = ["create"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "upstream-id",
                "status": "queued" if mode[0] == "create" else "completed",
                "created_at": 1,
            },
        )

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        mode[0] = "completed"

        def fail_update(*args, **kwargs):
            raise sqlite3.OperationalError("/private/router/video-jobs.sqlite3 leaked")

        monkeypatch.setattr("mesh.job_store.VideoJobStore.update_status", fail_update)
        response = client.get(f"/v1/videos/{created.json()['id']}")

    assert response.status_code == 502
    assert "private" not in response.text
    assert "sqlite" not in response.text.lower()


def test_video_create_preflight_punt_has_complete_audit_headers(tmp_path) -> None:
    response = TestClient(
        _video_app(
            _snapshot(capabilities=[]),
            lambda request: pytest.fail("capability punt reached upstream"),
            tmp_path / "video-jobs.sqlite3",
        )
    ).post(
        "/v1/videos",
        files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
    )

    assert response.status_code == 503
    assert response.headers["X-Slancha-Specialist"] == SPECIALIST_ID
    assert response.headers["X-Slancha-Node"] == "unselected"
    assert "vllm_omni.video.jobs.v1" in response.headers["X-Slancha-Reason"]


def test_video_content_overflow_fails_before_response_starts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"

    class UndeclaredStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        return httpx.Response(
            200,
            stream=UndeclaredStream(),
            headers={"content-type": "video/mp4"},
        )

    monkeypatch.setattr("mesh.router_app._VIDEO_CONTENT_MAX_BYTES", 8)
    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path),
        raise_server_exceptions=False,
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        response = client.get(f"/v1/videos/{created.json()['id']}/content")

    assert response.status_code == 502
    assert response.content != b"12345678"
    assert "response_too_large" in response.headers["X-Slancha-Reason"]


def test_concurrent_video_deletes_touch_owner_exactly_once(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    owner_entered = threading.Event()
    release_owner = threading.Event()
    delete_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal delete_calls
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        if request.method == "DELETE":
            delete_calls += 1
            owner_entered.set()
            for _ in range(200):
                if release_owner.is_set():
                    break
                await asyncio.sleep(0.01)
            assert release_owner.is_set()
            return httpx.Response(
                200,
                json={"id": "upstream-id", "deleted": True, "object": "video.deleted"},
            )
        raise AssertionError("unexpected owner request")

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        path = f"/v1/videos/{created.json()['id']}"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.delete, path)
            assert owner_entered.wait(timeout=2)
            second = pool.submit(client.delete, path)
            second_response = second.result(timeout=2)
            release_owner.set()
            first_response = first.result(timeout=2)

    assert first_response.status_code == 200
    assert second_response.status_code == 404
    assert delete_calls == 1


def test_video_delete_deadline_releases_claim_before_single_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    stream_entered = threading.Event()
    stream_closed = threading.Event()
    delete_calls = 0

    class DelayedDeleteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            stream_entered.set()
            await asyncio.sleep(0.25)
            yield json.dumps(
                {
                    "id": "upstream-id",
                    "deleted": True,
                    "object": "video.deleted",
                }
            ).encode()

        async def aclose(self) -> None:
            stream_closed.set()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal delete_calls
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        if request.method == "DELETE":
            delete_calls += 1
            if delete_calls == 1:
                return httpx.Response(
                    200,
                    stream=DelayedDeleteStream(),
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "upstream-id",
                    "deleted": True,
                    "object": "video.deleted",
                },
            )
        raise AssertionError("unexpected owner request")

    monkeypatch.setattr("mesh.router_app._VIDEO_DELETE_DEADLINE_S", 0.05)
    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        path = f"/v1/videos/{created.json()['id']}"
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(client.delete, path)
            assert stream_entered.wait(timeout=2)
            overlapping = client.delete(path)
            first_response = first.result(timeout=2)
        retry = client.delete(path)

    assert overlapping.status_code == 404
    assert first_response.status_code == 503
    assert first_response.json()["error"]["details"]["retryable"] is True
    assert "upstream_delete_timeout" in first_response.headers["X-Slancha-Reason"]
    assert stream_closed.is_set()
    assert retry.status_code == 200
    assert delete_calls == 2


def test_video_delete_requires_exact_upstream_200_and_releases_claim(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    mode = ["create"]

    def handler(request: httpx.Request) -> httpx.Response:
        if mode[0] == "create":
            return httpx.Response(
                200,
                json={"id": "upstream-id", "status": "queued", "created_at": 1},
            )
        return httpx.Response(
            202,
            json={"id": "upstream-id", "deleted": True, "object": "video.deleted"},
        )

    with TestClient(
        _video_app(_snapshot(capabilities=[VIDEO_JOB_CAPABILITY]), handler, db_path)
    ) as client:
        created = client.post(
            "/v1/videos",
            files=[("model", (None, SPECIALIST_ID)), ("prompt", (None, "prompt"))],
        )
        mode[0] = "delete"
        first = client.delete(f"/v1/videos/{created.json()['id']}")
        second = client.delete(f"/v1/videos/{created.json()['id']}")

    assert first.status_code == 502
    assert second.status_code == 502
