"""Tests for the registry durability seam (mesh.event_store + registry wiring).

Exercises the shared `_record` mechanism via the lightweight `record_node_left`
writer (the heartbeat/allocation/quality writers share the same path; the
existing e2e suite is the regression net for those).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mesh.event_store import EventEnvelope, NullEventStore
from mesh.registry import (
    AllocationEvent,
    HeartbeatPostRequest,
    MeshRegistry,
    NodeLeftEvent,
    QualityObservationEvent,
    _decode,
    _encode,
)
from mesh.tests.conftest import make_heartbeat


class FakeStore:
    """In-memory durable store stand-in: append accumulates, replay yields all."""

    def __init__(self) -> None:
        self.events: list[EventEnvelope] = []

    def append(self, env: EventEnvelope) -> None:
        self.events.append(env)

    def replay(self):
        return list(self.events)


class RaisingStore:
    """A store whose durable append fails (e.g. unrecoverable error)."""

    def append(self, env: EventEnvelope) -> None:
        raise RuntimeError("store down")

    def replay(self):
        return ()


_NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)


# ───────────────────────────── default behavior ─────────────────────────────


def test_default_store_is_in_memory_only():
    """No store → NullEventStore → identical pre-seam behavior."""
    reg = MeshRegistry()
    assert isinstance(reg._store, NullEventStore)
    reg.record_node_left("node-1", "bye")
    assert [e.kind for e in reg._events] == ["node_left"]


def test_null_store_replay_is_empty():
    assert list(NullEventStore().replay()) == []


# ───────────────────────────── codec round-trip ─────────────────────────────


def test_codec_round_trips_each_event_kind():
    events = [
        NodeLeftEvent(ts=_NOW, node_id="n1", reason="x"),
        AllocationEvent(ts=_NOW, strategy="diversify", suggestions={"n1": "spec-a"}),
        QualityObservationEvent(
            ts=_NOW, specialist_id="spec-a", score=3.5, sample_count=10,
            observation_source="synthetic",
        ),
    ]
    for ev in events:
        env = _encode(ev)
        assert env.kind == ev.kind and env.event_id  # registry-assigned id present
        assert _decode(env) == ev  # opaque payload round-trips losslessly


def test_decode_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown event kind"):
        _decode(EventEnvelope(event_id="x", kind="bogus", ts="2026", payload="{}"))


# ─────────────────── durable append + boot replay (the point) ────────────────


def test_durable_append_then_replay_rebuilds_state_across_restart():
    store = FakeStore()
    reg = MeshRegistry(store=store)
    reg.record_node_left("node-1", "a")
    reg.record_node_left("node-2", "b")
    assert len(store.events) == 2  # durably persisted

    # "Restart": a fresh registry over the same durable store replays at boot.
    reg2 = MeshRegistry(store=store)
    assert [e.kind for e in reg2._events] == ["node_left", "node_left"]
    assert [e.node_id for e in reg2._events] == ["node-1", "node-2"]  # order preserved


def test_replay_preserves_append_order():
    store = FakeStore()
    a = MeshRegistry(store=store)
    for i in range(5):
        a.record_node_left(f"n{i}", "x")
    b = MeshRegistry(store=store)
    assert [e.node_id for e in b._events] == [f"n{i}" for i in range(5)]


# ─────────────────── durable-first: no silent divergence ─────────────────────


def test_failed_durable_append_does_not_mutate_read_model():
    """Durable-FIRST: if the store raises, the in-memory read model is untouched
    (neither side has the event — no silent divergence; the caller sees it)."""
    reg = MeshRegistry(store=RaisingStore())
    with pytest.raises(RuntimeError, match="store down"):
        reg.record_node_left("node-1", "a")
    assert reg._events == []


# ─────────────────── durable-log compaction (bounds boot replay) ─────────────


class CompactingFakeStore(FakeStore):
    """FakeStore that also supports the optional compact() seam."""

    def __init__(self) -> None:
        super().__init__()
        self.compactions = 0

    def compact(self, envelopes) -> None:
        self.events = list(envelopes)
        self.compactions += 1


def test_durable_log_compacts_with_in_memory(spark_node, fresh_now, catalog):
    """When the registry compacts superseded heartbeats in-memory, it also
    compacts the durable log — so an append-only store stays bounded."""
    store = CompactingFakeStore()
    reg = MeshRegistry(catalog, max_events=2, store=store)
    for qd in range(5):  # 5 heartbeats from ONE node → all but latest superseded
        hb = make_heartbeat(spark_node, fresh_now, ["nemotron-math-7b-q4"], catalog, queue_depth=qd)
        reg.record_heartbeat(HeartbeatPostRequest(heartbeat=hb, node_url="http://spark-1:8000/v1"))

    assert store.compactions >= 1                 # the durable log was compacted
    assert len(store.events) <= 2                 # bounded (latest heartbeat kept)

    # A fresh registry over the compacted durable log rebuilds the live node.
    reg2 = MeshRegistry(catalog, store=store)
    snap = reg2.snapshot(now=fresh_now)
    assert len(snap.nodes) == 1


def test_store_without_compact_still_works(spark_node, fresh_now, catalog):
    """A durable store that omits compact() is simply left to grow (no crash)."""
    store = FakeStore()  # no compact attribute
    reg = MeshRegistry(catalog, max_events=2, store=store)
    for qd in range(4):
        hb = make_heartbeat(spark_node, fresh_now, ["nemotron-math-7b-q4"], catalog, queue_depth=qd)
        reg.record_heartbeat(HeartbeatPostRequest(heartbeat=hb, node_url="http://spark-1:8000/v1"))
    # in-memory compacted (bounded); durable store untouched by compaction → grew.
    assert len(reg._events) <= 2 and len(store.events) >= 4
