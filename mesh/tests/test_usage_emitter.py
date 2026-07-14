"""Slice B — mesh usage-emitter seam (mesh/usage.py + router taps + cli wiring).

Three layers, all offline (MockTransport upstream + tmp-dir spool — no real socket, no
real receiver):
  1. Pure builder / validation (no I/O): build_usage_event + _safe_count + _clean_user.
  2. Router taps in situ: NullSink no-op; non-stream + stream emit; telemetry never faults
     a completion; no emit on error paths; per-attempt latency; auto-route resolved id.
  3. SpoolDrainSink mechanics: single-event POST contract, fault-injection grow/drain,
     poison-row + corrupt-line isolation, mid-drain-append safety, overflow bound, lifespan.

Maps 1:1 to the success criteria in docs/F3-B-EMITTER-PLAN.md (prost repo).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mesh.router_app import create_router_app
from mesh.models import MeshSelectionResult, RegistrySnapshot
from mesh.tests.test_router_app import _binding, _card, _snapshot
from mesh.usage import (
    MAX_USER_ID_BYTES,
    NullSink,
    SpoolDrainSink,
    build_usage_event,
)

USAGE = {"prompt_tokens": 230, "completion_tokens": 1247}
SID = "qwen2.5-coder-7b-q4-ollama"


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
def _one_specialist_snap() -> RegistrySnapshot:
    return _snapshot(cards=[_card(specialist_id=SID)], bindings={SID: [_binding(specialist_id=SID)]})


class _RecordSink:
    """Captures emitted events in memory — for asserting emit content/shape."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.skipped_missing = 0

    def emit(self, event: dict) -> None:
        self.events.append(event)


class _RaisingSink:
    def emit(self, event: dict) -> None:
        raise RuntimeError("sink is down")


class _DrainableSink:
    """Duck-typed drainable — records lifespan start()/aclose() for SC18."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def emit(self, event: dict) -> None:
        return None

    def start(self) -> None:
        self.started = True

    async def aclose(self) -> None:
        self.closed = True


def _app(snapshot, handler, *, usage_sink=None, auto_router=None):
    def transport_handler(request: httpx.Request) -> httpx.Response:
        result = handler(request)
        if isinstance(result, httpx.Response):
            return result
        status_code, payload, headers = result
        if isinstance(payload, dict):
            return httpx.Response(status_code, json=payload, headers=headers or {})
        return httpx.Response(status_code, content=payload, headers=headers or {})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(transport_handler))
    return create_router_app(
        snapshot_source=lambda: snapshot,
        http_client=upstream,
        usage_sink=usage_sink,
        auto_router=auto_router,
    )


def _post(client, *, model=SID, stream=False, user=None):
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    if stream:
        body["stream"] = True
    if user is not None:
        body["user"] = user
    return client.post("/v1/chat/completions", json=body)


class _FakeAuto:
    def __init__(self, resolved: str) -> None:
        self._resolved = resolved

    def warmup(self) -> None:  # pragma: no cover - not called in tests
        pass

    def select(self, body: dict, snapshot: RegistrySnapshot) -> MeshSelectionResult:
        return MeshSelectionResult(
            model="Qwen/Qwen2.5-Coder-7B-Instruct",
            specialist_id=self._resolved,
            node_id="node-a",
            node_url="http://10.0.0.5:11434",
            reason="fake auto",
            queue_ms_estimated=0,
            cluster_coverage_used=True,
        )


def _receiver(status_of):
    """An httpx client (MockTransport) recording each POSTed event; `status_of(n)` gives
    the reply status for the n-th (1-based) POST."""
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        code = status_of(len(posts))
        return httpx.Response(code, json={"ok": code < 300, "deduped": False})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), posts


# ==========================================================================
# 1. Pure builder / validation
# ==========================================================================
def test_build_event_full_shape():
    ev = build_usage_event(
        specialist_id="spec-x", user_field="alice", status_code=200, latency_ms=1200,
        usage=USAGE, response_id="chatcmpl-9", fallback_fired=False,
    )
    for req in ("request_id", "user_id", "endpoint", "model", "route",
                "tokens_in", "tokens_out", "latency_ms", "status_code"):
        assert req in ev
    assert ev["model"] == "spec-x" and ev["specialist_id"] == "spec-x"
    assert ev["route"] == "mesh" and ev["endpoint"] == "/v1/chat/completions"
    assert ev["tokens_in"] == 230 and ev["tokens_out"] == 1247
    assert ev["request_id"] == "chatcmpl-9" and ev["user_id"] == "alice"
    assert ev["gen_ai.request.model"] == "spec-x"
    assert ev["gen_ai.usage.input_tokens"] == 230
    # honest split: mesh omits cost + ts (prost prices; ts ships with the wire bump)
    assert "cost_cents" not in ev and "cloud_equivalent_cost_cents" not in ev
    assert "ts" not in ev


def test_build_event_stamps_uuid_when_no_response_id():
    ev = build_usage_event(specialist_id="s", user_field=None, status_code=200,
                           latency_ms=1, usage=USAGE, response_id=None)
    assert ev["request_id"].startswith("req-") and len(ev["request_id"]) > 8


@pytest.mark.parametrize("usage", [
    None, {}, {"prompt_tokens": 1}, {"prompt_tokens": -5, "completion_tokens": 10},
    {"prompt_tokens": 1.5, "completion_tokens": 10}, {"prompt_tokens": "100", "completion_tokens": 10},
    {"prompt_tokens": True, "completion_tokens": 10}, {"prompt_tokens": 10, "completion_tokens": None},
    {"prompt_tokens": 1e309, "completion_tokens": 1}, {"prompt_tokens": 10, "completion_tokens": 10**12},
])
def test_safe_count_rejects_hostile_usage(usage):
    assert build_usage_event(specialist_id="s", user_field=None, status_code=200,
                             latency_ms=1, usage=usage) is None


@pytest.mark.parametrize("raw,expected", [
    ("alice", "alice"), ("  bob  ", "bob"), ("", "unattributed"), ("   ", "unattributed"),
    ("none", "unattributed"), ("NULL", "unattributed"), (None, "unattributed"), (123, "unattributed"),
    ("a" * (MAX_USER_ID_BYTES + 1), "unattributed"),  # oversized → not a real user
])
def test_clean_user_maps(raw, expected):
    ev = build_usage_event(specialist_id="s", user_field=raw, status_code=200,
                           latency_ms=1, usage=USAGE)
    assert ev["user_id"] == expected


def test_user_with_newline_never_breaks_the_spool_line(tmp_path):
    sink = SpoolDrainSink(tmp_path / "spool.jsonl", "http://x/v1/usage")
    ev = build_usage_event(specialist_id="s", user_field="a\nb\rc", status_code=200,
                           latency_ms=1, usage=USAGE)
    sink.emit(ev)
    lines = (tmp_path / "spool.jsonl").read_text().splitlines()
    assert len(lines) == 1  # json.dumps escaped the control chars — one line, no injection


# ==========================================================================
# 2. Router taps in situ
# ==========================================================================
def test_nullsink_default_no_behavior_change():
    snap = _one_specialist_snap()
    body = {"id": "chatcmpl-1", "choices": [{"message": {"content": "ok"}}], "usage": USAGE}
    app = _app(snap, lambda req: (200, body, None))  # no usage_sink → NullSink
    assert isinstance(app.state.usage_sink, NullSink)
    r = _post(TestClient(app))
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ok"


def test_nonstream_happy_emits_one_event():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    body = {"id": "chatcmpl-7", "choices": [{"message": {"content": "ok"}}], "usage": USAGE}
    app = _app(snap, lambda req: (200, body, None), usage_sink=sink)
    r = _post(TestClient(app), user="alice")
    assert r.status_code == 200
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev["route"] == "mesh" and ev["model"] == SID and ev["user_id"] == "alice"
    assert ev["tokens_in"] == 230 and ev["tokens_out"] == 1247
    assert ev["status_code"] == 200 and ev["request_id"] == "chatcmpl-7"
    assert ev["latency_ms"] >= 0 and ev["fallback_fired"] is False


def test_nonstream_missing_usage_skips():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    body = {"id": "x", "choices": [{"message": {"content": "ok"}}]}  # NO usage block
    app = _app(snap, lambda req: (200, body, None), usage_sink=sink)
    r = _post(TestClient(app))
    assert r.status_code == 200
    assert sink.events == [] and sink.skipped_missing == 1  # honest skip, no fake 0


def test_nonstream_malformed_upstream_body_does_not_fault_completion():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    # not-JSON bytes; the router forwards them verbatim, the tap must not crash the request
    app = _app(snap, lambda req: (200, b"<<not json>>", {"content-type": "text/plain"}),
               usage_sink=sink)
    r = _post(TestClient(app))
    assert r.status_code == 200 and r.content == b"<<not json>>"
    assert sink.events == []  # unparseable → treated as missing usage, skipped


def test_raising_sink_never_faults_nonstream_completion():
    snap = _one_specialist_snap()
    body = {"id": "x", "choices": [{"message": {"content": "ok"}}], "usage": USAGE}
    app = _app(snap, lambda req: (200, body, None), usage_sink=_RaisingSink())
    r = _post(TestClient(app))
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ok"


def _sse(*chunks: bytes) -> bytes:
    return b"".join(chunks)


def test_stream_emits_usage_from_final_chunk():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    sse = _sse(
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":4}}\n\n',
        b"data: [DONE]\n\n",
    )
    app = _app(snap, lambda req: httpx.Response(200, content=sse,
               headers={"content-type": "text/event-stream"}), usage_sink=sink)
    r = _post(TestClient(app), stream=True)
    assert r.status_code == 200 and r.content == sse
    assert len(sink.events) == 1
    assert sink.events[0]["tokens_in"] == 11 and sink.events[0]["tokens_out"] == 4
    assert sink.events[0]["ttft_ms"] is not None


def test_stream_without_include_usage_skips():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    sse = _sse(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n', b"data: [DONE]\n\n")
    app = _app(snap, lambda req: httpx.Response(200, content=sse,
               headers={"content-type": "text/event-stream"}), usage_sink=sink)
    r = _post(TestClient(app), stream=True)
    assert r.status_code == 200 and r.content == sse
    assert sink.events == [] and sink.skipped_missing == 1  # no counts → honest skip


def test_raising_sink_never_faults_stream_and_bytes_flow():
    snap = _one_specialist_snap()
    sse = _sse(
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
        b"data: [DONE]\n\n",
    )
    app = _app(snap, lambda req: httpx.Response(200, content=sse,
               headers={"content-type": "text/event-stream"}), usage_sink=_RaisingSink())
    r = _post(TestClient(app), stream=True)
    assert r.status_code == 200 and r.content == sse  # every byte, in order, unaffected


def test_no_emit_on_bad_request():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    app = _app(snap, lambda req: (200, {}, None), usage_sink=sink)
    r = TestClient(app).post("/v1/chat/completions", content=b"not json")
    assert r.status_code == 400 and sink.events == [] and sink.skipped_missing == 0


def test_no_emit_when_all_bindings_fail():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    app = _app(snap, lambda req: (503, {"error": "down"}, None), usage_sink=sink)
    r = _post(TestClient(app))
    assert r.status_code == 502 and sink.events == []  # never reached a winning completion


def test_no_emit_on_oversized_413(monkeypatch):
    import mesh.router_app as ra
    monkeypatch.setattr(ra, "MAX_REQUEST_BYTES", 10)  # tiny cap → any real body is 413
    snap = _one_specialist_snap()
    sink = _RecordSink()
    app = _app(snap, lambda req: (200, {}, None), usage_sink=sink)
    r = _post(TestClient(app))
    assert r.status_code == 413 and sink.events == []


def test_no_emit_on_no_reachable_node_404():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    app = _app(snap, lambda req: (200, {}, None), usage_sink=sink)
    r = _post(TestClient(app), model="ghost-specialist")  # not in snapshot → 404
    assert r.status_code == 404 and sink.events == []


def test_stream_auto_route_emits_resolved_specialist():
    # SC13 streaming half: model:"auto" over the STREAMING path must emit the resolved id.
    snap = _one_specialist_snap()
    sink = _RecordSink()
    sse = _sse(
        b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n',
        b"data: [DONE]\n\n",
    )
    app = _app(snap, lambda req: httpx.Response(200, content=sse,
               headers={"content-type": "text/event-stream"}), usage_sink=sink,
               auto_router=_FakeAuto(resolved=SID))
    r = _post(TestClient(app), model="auto", stream=True)
    assert r.status_code == 200 and r.content == sse
    assert len(sink.events) == 1
    assert sink.events[0]["model"] == SID and sink.events[0]["model"] != "auto"


def test_per_attempt_latency_measures_winning_call_only():
    # binding #1 is SLOW then returns a retriable 503; binding #2 wins fast. latency_ms must
    # reflect only the winning call — a single t0 before the retry loop would fold in the
    # 250ms failed detour. Also asserts fallback_fired.
    snap = _snapshot(
        cards=[_card(specialist_id=SID)],
        bindings={SID: [
            _binding(node_id="a", specialist_id=SID, node_url="http://10.0.0.5:11434"),
            _binding(node_id="b", specialist_id=SID, node_url="http://10.0.0.6:11434"),
        ]},
    )
    sink = _RecordSink()
    win = {"id": "x", "choices": [{"message": {"content": "ok"}}], "usage": USAGE}
    calls = {"n": 0}

    def handler(req: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.25)          # slow FAILING first attempt
            return (503, {}, None)
        return (200, win, None)       # fast WINNING second attempt

    app = _app(snap, handler, usage_sink=sink)
    r = _post(TestClient(app))
    assert r.status_code == 200
    ev = sink.events[0]
    assert ev["fallback_fired"] is True
    assert ev["latency_ms"] < 150  # winning call only, NOT the ~250ms detour


def test_auto_route_emits_resolved_specialist_not_literal_auto():
    snap = _one_specialist_snap()
    sink = _RecordSink()
    body = {"id": "x", "choices": [{"message": {"content": "ok"}}], "usage": USAGE}
    app = _app(snap, lambda req: (200, body, None), usage_sink=sink,
               auto_router=_FakeAuto(resolved=SID))
    r = _post(TestClient(app), model="auto")
    assert r.status_code == 200
    assert len(sink.events) == 1
    assert sink.events[0]["model"] == SID and sink.events[0]["specialist_id"] == SID
    assert sink.events[0]["model"] != "auto"


# ==========================================================================
# 3. SpoolDrainSink mechanics
# ==========================================================================
def test_emit_appends_and_drain_posts_one_event_per_line(tmp_path):
    client, posts = _receiver(lambda n: 200)
    sink = SpoolDrainSink(tmp_path / "s.jsonl", "http://r/v1/usage", http_client=client)
    for i in range(3):
        sink.emit(build_usage_event(specialist_id=SID, user_field=f"u{i}", status_code=200,
                                    latency_ms=10, usage=USAGE))
    assert len((tmp_path / "s.jsonl").read_text().splitlines()) == 3

    res = asyncio.run(sink.drain_once())
    assert res.delivered == 3 and res.posted == 3
    # each POST carried exactly ONE event object (matches prost's single-event /v1/usage)
    assert len(posts) == 3 and all(isinstance(p, dict) and "request_id" in p for p in posts)
    assert (tmp_path / "s.jsonl").read_text() == ""  # fully drained


def test_fault_injection_spool_grows_then_drains(tmp_path):
    down = {"v": True}

    def status_of(n):
        return 503 if down["v"] else 200

    client, posts = _receiver(status_of)
    sink = SpoolDrainSink(tmp_path / "s.jsonl", "http://r/v1/usage", http_client=client)
    for i in range(5):
        sink.emit(build_usage_event(specialist_id=SID, user_field=None, status_code=200,
                                    latency_ms=10, usage=USAGE))

    res = asyncio.run(sink.drain_once())  # receiver down → nothing delivered, spool intact
    assert res.delivered == 0 and res.stopped_transient is True
    assert len((tmp_path / "s.jsonl").read_text().splitlines()) == 5  # ZERO loss

    down["v"] = False
    res = asyncio.run(sink.drain_once())  # receiver back → drains to empty
    assert res.delivered == 5
    assert (tmp_path / "s.jsonl").read_text() == ""


def test_redelivery_is_byte_identical(tmp_path):
    # a 503 leaves the line; when it later delivers, the POSTed payload is identical
    # (stable request_id) → the receiver can dedup. Proves at-least-once honesty.
    def status_of(n):
        return 503  # always fail this receiver

    client, posts = _receiver(status_of)
    sink = SpoolDrainSink(tmp_path / "s.jsonl", "http://r/v1/usage", http_client=client)
    sink.emit(build_usage_event(specialist_id=SID, user_field="alice", status_code=200,
                                latency_ms=10, usage=USAGE, response_id="chatcmpl-stable"))
    asyncio.run(sink.drain_once())
    asyncio.run(sink.drain_once())
    assert len(posts) == 2 and posts[0] == posts[1]  # byte-identical redelivery
    assert posts[0]["request_id"] == "chatcmpl-stable"


def test_poison_row_isolation(tmp_path):
    # line 2 of 3 gets a 422 (permanent) → dropped, lines 1 & 3 delivered, spool empties.
    def status_of(n):
        return 422 if n == 2 else 200

    client, posts = _receiver(status_of)
    sink = SpoolDrainSink(tmp_path / "s.jsonl", "http://r/v1/usage", http_client=client)
    for i in range(3):
        sink.emit(build_usage_event(specialist_id=SID, user_field=f"u{i}", status_code=200,
                                    latency_ms=10, usage=USAGE))
    res = asyncio.run(sink.drain_once())
    assert res.delivered == 2 and res.poison == 1 and sink.poison_dropped == 1
    assert (tmp_path / "s.jsonl").read_text() == ""  # poison did NOT wedge the FIFO


def test_corrupt_spool_line_dropped(tmp_path):
    spool = tmp_path / "s.jsonl"
    spool.write_text('{"bad json\n' + json.dumps(
        build_usage_event(specialist_id=SID, user_field=None, status_code=200,
                          latency_ms=10, usage=USAGE)) + "\n")
    client, posts = _receiver(lambda n: 200)
    sink = SpoolDrainSink(spool, "http://r/v1/usage", http_client=client)
    res = asyncio.run(sink.drain_once())
    assert res.corrupt == 1 and sink.corrupt_lines == 1 and res.delivered == 1
    assert spool.read_text() == ""  # corrupt line skipped, good line delivered, none wedged


def test_drain_preserves_line_appended_mid_drain(tmp_path):
    spool = tmp_path / "s.jsonl"
    ev_b = build_usage_event(specialist_id=SID, user_field="B", status_code=200,
                             latency_ms=10, usage=USAGE, response_id="B")

    def handler(request: httpx.Request) -> httpx.Response:
        # Simulate a concurrent emit DURING the network await of line A's POST. This works
        # only if the drain released the lock before awaiting. NOTE: a lock-across-await
        # regression would DEADLOCK the single-threaded loop synchronously — the wait_for
        # below CANNOT fire (its callback can't run on the blocked thread), so such a
        # regression hangs; the CI job's timeout-minutes is what bounds it, not this test.
        sink.emit(ev_b)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = SpoolDrainSink(spool, "http://r/v1/usage", http_client=client)
    sink.emit(build_usage_event(specialist_id=SID, user_field="A", status_code=200,
                                latency_ms=10, usage=USAGE, response_id="A"))

    async def _run():
        return await asyncio.wait_for(sink.drain_once(), timeout=5)

    res = asyncio.run(_run())
    assert res.delivered == 1  # only A was in the snapshot
    remaining = [json.loads(x) for x in spool.read_text().splitlines()]
    assert len(remaining) == 1 and remaining[0]["request_id"] == "B"  # B kept, A dropped


def test_overflow_bounded_drops_newest_when_full(tmp_path):
    # Bounded FIFO: emit is append-or-drop-NEWEST (O(1), never a head rewrite — so it can't
    # race the drain's line removal or stall the event loop with O(spool) I/O). Once full,
    # later events are refused with a counter; the spool never exceeds the hard cap.
    spool = tmp_path / "s.jsonl"
    sink = SpoolDrainSink(spool, "http://r/v1/usage", soft_cap_bytes=200, hard_cap_bytes=1200)
    for i in range(60):
        sink.emit(build_usage_event(specialist_id=SID, user_field=f"user-{i:03d}", status_code=200,
                                    latency_ms=10, usage=USAGE, response_id=f"id-{i:03d}"))
    assert sink.overflow_dropped > 0
    assert spool.stat().st_size <= 1200  # never exceeds the hard cap
    ids = [json.loads(x)["request_id"] for x in spool.read_text().splitlines()]
    assert "id-000" in ids and "id-059" not in ids  # earliest kept, newest-past-full refused


def test_concurrent_overflow_during_drain_loses_no_undelivered_event(tmp_path):
    # gate-#2 BLOCKER regression: a mutation during the drain's network await must not make
    # the final rewrite erase an un-delivered event. emit A; during A's in-flight POST a
    # burst of emits runs (crossing the tiny hard cap → some refused). A delivers → removed
    # BY IDENTITY; every surviving line is a real un-posted burst event, never erased by a
    # positional-slice accident.
    spool = tmp_path / "s.jsonl"

    def handler(request: httpx.Request) -> httpx.Response:
        for i in range(20):
            sink.emit(build_usage_event(specialist_id=SID, user_field=None, status_code=200,
                                        latency_ms=10, usage=USAGE, response_id=f"flood-{i:02d}"))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = SpoolDrainSink(spool, "http://r/v1/usage", http_client=client, hard_cap_bytes=1500)
    sink.emit(build_usage_event(specialist_id=SID, user_field=None, status_code=200,
                                latency_ms=10, usage=USAGE, response_id="A"))

    res = asyncio.run(asyncio.wait_for(sink.drain_once(), timeout=5))
    assert res.delivered == 1
    remaining = [json.loads(x)["request_id"] for x in spool.read_text().splitlines()]
    assert "A" not in remaining                      # delivered → removed by identity
    assert all(r.startswith("flood-") for r in remaining)  # every survivor is a real un-posted event
    assert len(remaining) >= 1                        # NOT silently erased by a positional slice


def test_lifespan_starts_and_stops_drainable_sink():
    snap = _one_specialist_snap()
    sink = _DrainableSink()
    app = _app(snap, lambda req: (200, {}, None), usage_sink=sink)
    assert sink.started is False
    with TestClient(app):  # __enter__ fires lifespan startup, __exit__ fires shutdown
        assert sink.started is True
    assert sink.closed is True


def test_lifespan_noop_for_nullsink():
    # existing no-arg router construction must still behave with the lifespan present
    snap = _one_specialist_snap()
    app = _app(snap, lambda req: (200, {}, None))
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200


# ==========================================================================
# cli wiring — env → SpoolDrainSink (the real caller; no shelf-ware)
# ==========================================================================
def test_usage_sink_from_env_off_when_url_unset(monkeypatch):
    from mesh.cli import _usage_sink_from_env
    monkeypatch.delenv("SLANCHA_USAGE_SINK_URL", raising=False)
    assert _usage_sink_from_env() is None


def test_usage_sink_from_env_builds_sink_with_params(monkeypatch, tmp_path):
    from mesh.cli import _usage_sink_from_env
    spool = tmp_path / "u.jsonl"
    monkeypatch.setenv("SLANCHA_USAGE_SINK_URL", "http://127.0.0.1:8977/v1/usage")
    monkeypatch.setenv("SLANCHA_USAGE_SINK_TOKEN", "tok-xyz")
    monkeypatch.setenv("SLANCHA_USAGE_SPOOL_PATH", str(spool))
    monkeypatch.setenv("SLANCHA_USAGE_DRAIN_INTERVAL_S", "9")
    sink = _usage_sink_from_env()
    assert isinstance(sink, SpoolDrainSink)
    assert sink.spool == spool and sink._url == "http://127.0.0.1:8977/v1/usage"
    assert sink._token == "tok-xyz" and sink._interval == 9.0


def test_usage_sink_from_env_bad_interval_falls_back(monkeypatch):
    from mesh.cli import _usage_sink_from_env
    monkeypatch.setenv("SLANCHA_USAGE_SINK_URL", "http://x/v1/usage")
    monkeypatch.setenv("SLANCHA_USAGE_DRAIN_INTERVAL_S", "not-a-number")
    sink = _usage_sink_from_env()
    assert sink is not None and sink._interval == 5.0  # malformed → default, no crash


# ==========================================================================
# schema conformance (SC5) + OSS cleanliness (SC19)
# ==========================================================================
def _shared_schema():
    env = os.environ.get("SLANCHA_SHARED_PATH")
    root = Path(env) if env else Path(__file__).resolve().parents[2].parent / "slancha-shared"
    p = root / "schemas" / "mesh-usage-event.schema.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_emitted_event_validates_against_canonical_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _shared_schema()
    if schema is None:
        pytest.skip("sibling slancha-shared checkout not found")
    ev = build_usage_event(specialist_id=SID, user_field="alice", status_code=200,
                           latency_ms=1200, usage=USAGE, response_id="chatcmpl-9",
                           ttft_ms=120, fallback_fired=False)
    jsonschema.validate(ev, schema)  # the wire producer conforms to the wire contract


def test_no_prost_symbol_in_touched_mesh_source():
    root = Path(__file__).resolve().parents[1]
    for rel in ("usage.py", "router_app.py", "cli.py"):
        text = (root / rel).read_text().lower()
        assert "prost" not in text, f"prost symbol leaked into OSS file mesh/{rel}"
