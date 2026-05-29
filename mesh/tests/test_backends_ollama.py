"""OllamaBackend tests — mocked daemon, no live Ollama required.

These pin the contract OllamaBackend has with the running daemon:

  - start() refuses to launch without an `ollama_tag` on the card.
  - start() refuses when the daemon isn't reachable (clear error message).
  - wait_ready() polls `/api/tags` until the model lands, then `/api/generate`
    to pre-warm and confirm Ollama accepted the load.
  - utilization() parses `/api/ps` into the heartbeat shape; failures fall
    back to zeros rather than raising into the heartbeat loop.
  - stop() unloads the model via `keep_alive: 0` but does NOT kill the
    daemon; idempotent on a never-started backend.

The integration with a real `ollama serve` lives behind an env guard, the
same way `test_integration_vllm` does — that's where pull-then-load gets
exercised end-to-end on a Mac / Linux box.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mesh.backends import DEFAULT_OLLAMA_PORT, OllamaBackend
from mesh.models import SpecialistCard


def _card(*, ollama_tag: str | None = "qwen2.5-coder:7b") -> SpecialistCard:
    return SpecialistCard(
        model_id="Qwen/Qwen2.5-Coder-7B-Instruct",
        specialist_id="qwen2.5-coder-7b",
        domain="code",
        difficulty_tiers=["medium"],
        required_backend="ollama",
        storage_gb=5.0,
        runtime_gb=6.0,
        min_vram_gb=8.0,
        context_window=32768,
        n_layers=28,
        estimated_tps_at={"gb10": 40.0},
        ollama_tag=ollama_tag,
    )


class _MockDaemon:
    """Records httpx calls + replies from a routing table.

    Each entry: `("GET" | "POST", path) -> (status, json_body)` or
    `(status, lambda: dict)` so a test can flip state between calls.
    Anything not in the table 404s.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def reply(self, method: str, path: str, status: int = 200, body: Any | None = None) -> None:
        self.routes[(method.upper(), path)] = (status, body)

    def _resolve(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        path = httpx.URL(url).path or "/"
        self.calls.append((method.upper(), path, kwargs.get("json")))
        entry = self.routes.get((method.upper(), path))
        if entry is None:
            return httpx.Response(404, json={"error": "not found"})
        status, body = entry
        if callable(body):
            body = body()
        return httpx.Response(status, json=body)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "mesh.backends.httpx.get",
            lambda url, **kw: self._resolve("GET", url, **kw),
        )
        monkeypatch.setattr(
            "mesh.backends.httpx.post",
            lambda url, **kw: self._resolve("POST", url, **kw),
        )


def _make(daemon: _MockDaemon, **kw: Any) -> OllamaBackend:
    # Pinhole port so the test never collides with a real local Ollama.
    return OllamaBackend(card=_card(**kw), host="127.0.0.1", port=11498)


def test_default_port_constant_matches_ollama_convention():
    """11434 is the upstream-documented default; mesh must not silently drift."""
    assert DEFAULT_OLLAMA_PORT == 11434


def test_construction_exposes_base_url_and_name():
    be = OllamaBackend(card=_card(), host="10.0.0.5", port=11434)
    assert be.base_url == "http://10.0.0.5:11434"
    assert be.health_url == "http://10.0.0.5:11434/"
    assert be.name == "ollama"


def test_start_refuses_when_ollama_tag_missing(monkeypatch):
    """Catalog must map the specialist to an Ollama tag — be explicit."""
    daemon = _MockDaemon()
    daemon.reply("GET", "/", body={"status": "ok"})  # daemon up, but card incomplete
    daemon.install(monkeypatch)
    be = _make(daemon, ollama_tag=None)
    with pytest.raises(RuntimeError, match="ollama_tag"):
        be.start()
    assert not be.is_alive()


def test_start_refuses_when_daemon_unreachable(monkeypatch):
    """Operator should get a fix hint, not a silent NullBackend-like no-op."""
    daemon = _MockDaemon()  # nothing registered → / 404 → daemon "down"
    daemon.install(monkeypatch)
    be = _make(daemon)
    with pytest.raises(RuntimeError, match="ollama serve"):
        be.start()


def test_start_kicks_pull_when_model_missing(monkeypatch):
    """Start should be non-blocking on weights; pull happens off-thread."""
    daemon = _MockDaemon()
    daemon.reply("GET", "/", body={"status": "ok"})
    daemon.reply("GET", "/api/tags", body={"models": []})  # not yet pulled
    daemon.reply("POST", "/api/pull", body={"status": "pulling"})
    daemon.install(monkeypatch)
    be = _make(daemon)
    be.start()
    assert be.is_alive()
    # The pull call must have fired exactly once with the card's tag.
    pull_calls = [c for c in daemon.calls if c[:2] == ("POST", "/api/pull")]
    assert len(pull_calls) == 1
    assert pull_calls[0][2] == {"name": "qwen2.5-coder:7b", "stream": False}


def test_wait_ready_succeeds_once_model_pulls_and_prewarms(monkeypatch):
    """Polls /api/tags → /api/generate; returns True only after both succeed."""
    daemon = _MockDaemon()
    daemon.reply("GET", "/", body={"status": "ok"})
    pulled = {"flag": False}

    def tags() -> dict:
        return {"models": [{"name": "qwen2.5-coder:7b"}]} if pulled["flag"] else {"models": []}

    daemon.routes[("GET", "/api/tags")] = (200, tags)
    daemon.reply("POST", "/api/pull", body={"status": "pulling"})
    daemon.reply("POST", "/api/generate", body={"done": True})
    daemon.install(monkeypatch)
    # Skip the 2-second poll sleep so the test is fast.
    monkeypatch.setattr("mesh.backends.time.sleep", lambda _s: None)
    be = _make(daemon)
    be.start()
    pulled["flag"] = True  # weights "appear"
    assert be.wait_ready(timeout=5.0) is True

    # Pre-warm must use keep_alive from the backend, not the raw "0" unload.
    warm_calls = [c for c in daemon.calls if c[:2] == ("POST", "/api/generate")]
    assert warm_calls and warm_calls[-1][2]["keep_alive"] == "30m"


def test_wait_ready_returns_false_if_model_never_pulls(monkeypatch):
    """No flakiness: a timed-out pull is a False, not a raise."""
    daemon = _MockDaemon()
    daemon.reply("GET", "/", body={"status": "ok"})
    daemon.reply("GET", "/api/tags", body={"models": []})  # never lands
    daemon.reply("POST", "/api/pull", body={"status": "pulling"})
    daemon.install(monkeypatch)
    monkeypatch.setattr("mesh.backends.time.sleep", lambda _s: None)
    monkeypatch.setattr("mesh.backends.time.time", _fake_clock([0, 0.1, 0.2, 999.0]))
    be = _make(daemon)
    be.start()
    assert be.wait_ready(timeout=1.0) is False


def test_utilization_parses_loaded_model_as_running(monkeypatch):
    """`running` flips on iff our tag appears in /api/ps; gpu_cache_pct from size."""
    daemon = _MockDaemon()
    daemon.reply(
        "GET",
        "/api/ps",
        body={
            "models": [
                {
                    "name": "qwen2.5-coder:7b",
                    "size": 8_000_000_000,
                    "size_vram": 6_000_000_000,
                }
            ]
        },
    )
    daemon.install(monkeypatch)
    be = _make(daemon)
    util = be.utilization()
    assert util["running"] == 1
    assert util["queue_depth"] == 0  # Ollama publishes no queue gauge
    assert abs(util["gpu_cache_pct"] - 0.75) < 1e-9


def test_utilization_when_tag_not_loaded_reports_idle(monkeypatch):
    daemon = _MockDaemon()
    daemon.reply("GET", "/api/ps", body={"models": [{"name": "some-other:7b"}]})
    daemon.install(monkeypatch)
    be = _make(daemon)
    util = be.utilization()
    assert util == {"queue_depth": 0, "running": 0, "gpu_cache_pct": 0.0}


def test_utilization_swallows_daemon_errors(monkeypatch):
    """A wedged /api/ps must not raise into the heartbeat loop."""

    def _boom(_url: str, **_kw: Any) -> httpx.Response:
        raise httpx.ConnectError("daemon hiccup")

    monkeypatch.setattr("mesh.backends.httpx.get", _boom)
    be = OllamaBackend(card=_card(), host="127.0.0.1", port=11498)
    assert be.utilization() == {"queue_depth": 0, "running": 0, "gpu_cache_pct": 0.0}


def test_utilization_with_malformed_size_does_not_raise(monkeypatch):
    """Future Ollama could ship strings or nulls — we degrade to 0.0, not raise."""
    daemon = _MockDaemon()
    daemon.reply(
        "GET",
        "/api/ps",
        body={"models": [{"name": "qwen2.5-coder:7b", "size": "lots", "size_vram": None}]},
    )
    daemon.install(monkeypatch)
    be = _make(daemon)
    util = be.utilization()
    assert util["running"] == 1
    assert util["gpu_cache_pct"] == 0.0


def test_stop_unloads_via_keep_alive_zero_not_kill(monkeypatch):
    """Stop must release VRAM via the API, NOT SIGTERM the daemon."""
    daemon = _MockDaemon()
    daemon.reply("GET", "/", body={"status": "ok"})
    daemon.reply("GET", "/api/tags", body={"models": [{"name": "qwen2.5-coder:7b"}]})
    daemon.reply("POST", "/api/generate", body={"done": True})
    daemon.install(monkeypatch)
    monkeypatch.setattr("mesh.backends.time.sleep", lambda _s: None)
    be = _make(daemon)
    be.start()
    be.stop()
    # Last /api/generate must request unload, and the daemon stays untouched.
    gens = [c for c in daemon.calls if c[:2] == ("POST", "/api/generate")]
    assert gens, "stop() never called /api/generate"
    assert gens[-1][2]["keep_alive"] == 0


def test_stop_is_idempotent_when_never_started(monkeypatch):
    """A NullBackend-shape contract: stop() before start() = no-op."""
    daemon = _MockDaemon()
    daemon.install(monkeypatch)
    be = _make(daemon)
    be.stop()  # must not raise
    assert not be.is_alive()


def test_stop_after_daemon_dies_does_not_raise(monkeypatch):
    """Crashed daemon shouldn't turn `stop()` into an unhandled exception."""

    def _boom(_url: str, **_kw: Any) -> httpx.Response:
        raise httpx.ConnectError("daemon dead")

    # Start succeeds via a healthy mock; then we swap to the boom transport.
    daemon = _MockDaemon()
    daemon.reply("GET", "/", body={"status": "ok"})
    daemon.reply("GET", "/api/tags", body={"models": [{"name": "qwen2.5-coder:7b"}]})
    daemon.install(monkeypatch)
    be = _make(daemon)
    be.start()
    monkeypatch.setattr("mesh.backends.httpx.post", _boom)
    be.stop()  # must not raise
    assert not be.is_alive()


def _fake_clock(values: list[float]):
    """Returns a callable that yields the next value each invocation, last value forever."""
    seq = iter(values)
    last = values[-1]

    def _tick() -> float:
        nonlocal last
        try:
            last = next(seq)
        except StopIteration:
            pass
        return last

    return _tick
