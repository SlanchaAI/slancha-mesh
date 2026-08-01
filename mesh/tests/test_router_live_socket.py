"""Real-TCP proof for local success, typed punt, and circuit recovery."""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import httpx
import uvicorn
from fastapi import FastAPI, Response

from mesh.discovery import DiscoveredSpecialist, DiscoveryResult
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
