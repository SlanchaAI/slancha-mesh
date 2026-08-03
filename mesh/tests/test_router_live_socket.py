"""Real-TCP proof for local success, typed punt, and circuit recovery."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

from mesh.discovery import DiscoveredSpecialist, DiscoveryResult
from mesh.models import SpecialistCard
from mesh.router_app import create_router_app, discovery_to_snapshot
from mesh.runtime_health import RouterRuntimeHealth


@contextmanager
def _serve_live_app(app: FastAPI) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError("uvicorn test server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive(), "uvicorn test server did not stop"


def test_live_socket_local_success_punt_suppression_and_recovery() -> None:
    specialist_id = "live-local"
    now = [100.0]
    upstream_state = {"healthy": True, "calls": 0}
    upstream = FastAPI()

    @upstream.post("/v1/chat/completions")
    async def complete():
        upstream_state["calls"] += 1
        if not upstream_state["healthy"]:
            return Response(status_code=503)
        return {
            "id": "chatcmpl-live",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "LOCAL-OK"},
                    "finish_reason": "stop",
                }
            ],
        }

    with _serve_live_app(upstream) as upstream_url:
        discovery = DiscoveryResult(
            specialists={
                specialist_id: DiscoveredSpecialist(
                    specialist_id=specialist_id,
                    node_urls=(upstream_url,),
                )
            }
        )
        snapshot = discovery_to_snapshot(discovery)
        runtime = RouterRuntimeHealth(
            failure_threshold=2,
            cooldown_s=30,
            clock=lambda: now[0],
        )
        upstream_client = httpx.AsyncClient(timeout=2)
        router = create_router_app(
            snapshot_source=lambda: snapshot,
            http_client=upstream_client,
            runtime_health=runtime,
        )

        with _serve_live_app(router) as router_url, httpx.Client(timeout=2) as client:
            request = {
                "model": specialist_id,
                "messages": [{"role": "user", "content": "prove the route"}],
            }

            success = client.post(f"{router_url}/v1/chat/completions", json=request)
            assert success.status_code == 200
            assert success.json()["choices"][0]["message"]["content"] == "LOCAL-OK"

            upstream_state["healthy"] = False
            first_punt = client.post(f"{router_url}/v1/chat/completions", json=request)
            second_punt = client.post(f"{router_url}/v1/chat/completions", json=request)
            assert first_punt.headers["X-Slancha-Outcome"] == "punt"
            assert second_punt.json()["error"]["type"] == "slancha_punt"
            calls_after_open = upstream_state["calls"]

            suppressed = client.post(f"{router_url}/v1/chat/completions", json=request)
            assert suppressed.json()["error"]["details"]["local_attempts"] == 0
            assert upstream_state["calls"] == calls_after_open

            degraded = client.get(f"{router_url}/health").json()
            assert degraded["status"] == "degraded"
            assert degraded["specialists_reachable"] == 1
            assert degraded["specialists_routable"] == 0
            assert degraded["open_circuits"] == 1

            upstream_state["healthy"] = True
            now[0] += 31
            recovered = client.post(f"{router_url}/v1/chat/completions", json=request)
            assert recovered.status_code == 200
            assert recovered.json()["choices"][0]["message"]["content"] == "LOCAL-OK"

            healthy = client.get(f"{router_url}/health").json()
            assert healthy["status"] == "ok"
            assert healthy["specialists_routable"] == 1
            assert healthy["open_circuits"] == 0


def test_live_socket_runtime_shaped_json_multipart_and_binary_routes(
    monkeypatch,
) -> None:
    specialist_id = "live-media"
    upstream_model = "upstream-media-model"
    observed: dict[str, dict[str, object]] = {}
    upstream = FastAPI()

    def request_metadata(request: Request) -> dict[str, object]:
        return {
            "method": request.method,
            "path": request.url.path,
            "authorization": request.headers.get("authorization"),
            "content_type": request.headers.get("content-type"),
        }

    @upstream.post("/v1/images/generations")
    async def generate_image(request: Request):
        observed["image"] = {
            **request_metadata(request),
            "body": await request.json(),
        }
        return {"created": 1, "data": [{"b64_json": "aW1hZ2U="}]}

    @upstream.post("/v1/audio/speech")
    async def synthesize_speech(request: Request):
        observed["speech"] = {
            **request_metadata(request),
            "body": await request.json(),
        }
        return Response(content=b"RIFF-live-speech", media_type="audio/wav")

    @upstream.post("/v1/audio/transcriptions")
    async def transcribe_audio(request: Request):
        form = await request.form()
        upload = form["file"]
        observed["transcription"] = {
            **request_metadata(request),
            "model": form["model"],
            "filename": upload.filename,
            "file_content_type": upload.content_type,
            "file_bytes": await upload.read(),
        }
        return {"text": "runtime-shaped-stub"}

    with _serve_live_app(upstream) as upstream_url:
        discovery = DiscoveryResult(
            specialists={
                specialist_id: DiscoveredSpecialist(
                    specialist_id=specialist_id,
                    node_urls=(upstream_url,),
                )
            }
        )
        card = SpecialistCard(
            model_id="example/media",
            specialist_id=specialist_id,
            domain="general",
            difficulty_tiers=["medium"],
            required_backend="external",
            served_model_name=upstream_model,
            storage_gb=1,
            runtime_gb=1,
            min_vram_gb=1,
            context_window=4096,
            n_layers=1,
            estimated_tps_at={"test": 1},
            capabilities=[
                "protocol:openai.images.generations.v1",
                "protocol:openai.audio.speech.v1",
                "protocol:openai.audio.transcriptions.v1",
            ],
        )
        snapshot = discovery_to_snapshot(discovery, catalog=[card])
        monkeypatch.setenv("SLANCHA_UPSTREAM_TOKEN", "upstream-only")
        router = create_router_app(snapshot_source=lambda: snapshot)

        with _serve_live_app(router) as router_url, httpx.Client(timeout=2) as client:
            caller_headers = {"Authorization": "Bearer caller-secret"}
            image = client.post(
                f"{router_url}/v1/images/generations",
                headers=caller_headers,
                json={"model": specialist_id, "prompt": "red cube"},
            )
            speech = client.post(
                f"{router_url}/v1/audio/speech",
                headers=caller_headers,
                json={"model": specialist_id, "input": "hello"},
            )
            transcription = client.post(
                f"{router_url}/v1/audio/transcriptions",
                headers=caller_headers,
                files={
                    "model": (None, specialist_id),
                    "file": ("sample.wav", b"RIFF-live-input", "audio/wav"),
                },
            )

    assert image.status_code == 200
    assert speech.status_code == 200
    assert speech.headers["content-type"].startswith("audio/wav")
    assert speech.content == b"RIFF-live-speech"
    assert transcription.status_code == 200
    assert {
        image.headers["X-Slancha-Specialist"],
        speech.headers["X-Slancha-Specialist"],
        transcription.headers["X-Slancha-Specialist"],
    } == {specialist_id}

    for route in observed.values():
        assert route["method"] == "POST"
        assert route["authorization"] == "Bearer upstream-only"
        assert route["authorization"] != "Bearer caller-secret"
    assert observed["image"]["path"] == "/v1/images/generations"
    assert observed["image"]["content_type"] == "application/json"
    assert observed["image"]["body"]["model"] == upstream_model
    assert observed["speech"]["path"] == "/v1/audio/speech"
    assert observed["speech"]["content_type"] == "application/json"
    assert observed["speech"]["body"]["model"] == upstream_model
    assert observed["transcription"]["path"] == "/v1/audio/transcriptions"
    assert str(observed["transcription"]["content_type"]).startswith(
        "multipart/form-data; boundary="
    )
    assert observed["transcription"]["model"] == upstream_model
    assert observed["transcription"]["filename"] == "sample.wav"
    assert observed["transcription"]["file_content_type"] == "audio/wav"
    assert observed["transcription"]["file_bytes"] == b"RIFF-live-input"


def test_live_socket_video_owner_survives_router_restart(tmp_path) -> None:
    specialist_id = "live-video"
    upstream = FastAPI()
    decoy = FastAPI()
    upstream_calls: list[tuple[str, str]] = []
    decoy_calls: list[tuple[str, str]] = []

    @upstream.post("/v1/videos")
    async def create_video(request: Request):
        form = await request.form()
        upstream_calls.append(("POST", "/v1/videos"))
        assert form["model"] == specialist_id
        assert form["prompt"] == "restart proof"
        return {
            "id": "video_gen_live_123",
            "object": "video",
            "status": "queued",
            "created_at": 1701234567,
        }

    @upstream.get("/v1/videos/video_gen_live_123")
    async def poll_video():
        upstream_calls.append(("GET", "/v1/videos/video_gen_live_123"))
        return {
            "id": "video_gen_live_123",
            "object": "video",
            "status": "completed",
            "progress": 100,
            "created_at": 1701234567,
        }

    @upstream.get("/v1/videos/video_gen_live_123/content")
    async def download_video():
        upstream_calls.append(("GET", "/v1/videos/video_gen_live_123/content"))
        return Response(content=b"live-video-bytes", media_type="video/mp4")

    @upstream.delete("/v1/videos/video_gen_live_123")
    async def delete_video():
        upstream_calls.append(("DELETE", "/v1/videos/video_gen_live_123"))
        return {
            "id": "video_gen_live_123",
            "deleted": True,
            "object": "video.deleted",
        }

    @decoy.api_route(
        "/v1/videos",
        methods=["POST"],
    )
    @decoy.api_route(
        "/v1/videos/{remainder:path}",
        methods=["GET", "DELETE"],
    )
    async def decoy_video(request: Request):
        decoy_calls.append((request.method, request.url.path))
        return Response(status_code=500)

    with (
        _serve_live_app(upstream) as upstream_url,
        _serve_live_app(decoy) as decoy_url,
    ):
        initial_discovery = DiscoveryResult(
            specialists={
                specialist_id: DiscoveredSpecialist(
                    specialist_id=specialist_id,
                    node_urls=(upstream_url, decoy_url),
                )
            }
        )
        reordered_discovery = DiscoveryResult(
            specialists={
                specialist_id: DiscoveredSpecialist(
                    specialist_id=specialist_id,
                    node_urls=(decoy_url, upstream_url),
                )
            }
        )
        card = SpecialistCard(
            model_id="example/video",
            specialist_id=specialist_id,
            domain="general",
            difficulty_tiers=["medium"],
            required_backend="external",
            served_model_name=specialist_id,
            storage_gb=1,
            runtime_gb=1,
            min_vram_gb=1,
            context_window=4096,
            n_layers=1,
            estimated_tps_at={"test": 1},
            capabilities=["protocol:vllm_omni.video.jobs.v1"],
        )
        initial_snapshot = discovery_to_snapshot(initial_discovery, catalog=[card])
        reordered_snapshot = discovery_to_snapshot(reordered_discovery, catalog=[card])
        db_path = tmp_path / "router" / "video-jobs.sqlite3"

        first_router = create_router_app(
            snapshot_source=lambda: initial_snapshot,
            video_job_db_path=db_path,
        )
        with _serve_live_app(first_router) as router_url, httpx.Client(timeout=2) as client:
            created = client.post(
                f"{router_url}/v1/videos",
                files={
                    "model": (None, specialist_id),
                    "prompt": (None, "restart proof"),
                },
            )
            assert created.status_code == 200, created.text
            public_id = created.json()["id"]
            owner_node = created.headers["X-Slancha-Node"]
        restarted_router = create_router_app(
            snapshot_source=lambda: reordered_snapshot,
            video_job_db_path=db_path,
        )
        with _serve_live_app(restarted_router) as router_url, httpx.Client(timeout=2) as client:
            polled = client.get(f"{router_url}/v1/videos/{public_id}")
            content = client.get(f"{router_url}/v1/videos/{public_id}/content")
            deleted = client.delete(f"{router_url}/v1/videos/{public_id}")
            deleted_again = client.delete(f"{router_url}/v1/videos/{public_id}")
            gone = client.get(f"{router_url}/v1/videos/{public_id}")
    assert polled.status_code == 200
    assert polled.headers["X-Slancha-Node"] == owner_node
    assert polled.json()["id"] == public_id
    assert polled.json()["status"] == "completed"
    assert content.content == b"live-video-bytes"
    assert deleted.json() == {
        "id": public_id,
        "deleted": True,
        "object": "video.deleted",
    }
    assert deleted_again.status_code == 404
    assert gone.status_code == 404
    assert upstream_calls == [
        ("POST", "/v1/videos"),
        ("GET", "/v1/videos/video_gen_live_123"),
        ("GET", "/v1/videos/video_gen_live_123/content"),
        ("DELETE", "/v1/videos/video_gen_live_123"),
    ]
    assert decoy_calls == []
