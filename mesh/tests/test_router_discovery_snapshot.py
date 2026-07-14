"""Tests for the pull-mode glue in `mesh.router_app`:

- `discovery_to_snapshot` — translates a `DiscoveryResult` into a
  `RegistrySnapshot` the router can consume.
- `_RefreshingSnapshot` — caches a snapshot + refreshes it on a fixed
  cadence in a daemon thread.

These two together are what lets `slancha-mesh router` run without a
central registry: discovery is the source of truth, the router is its
OpenAI-compatible surface.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from mesh.discovery import DiscoveredSpecialist, DiscoveryResult
from mesh.models import NodeBinding, NodeSummary, RegistrySnapshot, SpecialistCard
from mesh.router_app import _RefreshingSnapshot, discovery_to_snapshot


def _card(specialist_id: str, *, ollama_tag: str | None = "qwen2.5-coder:7b") -> SpecialistCard:
    return SpecialistCard(
        model_id="Qwen/Qwen2.5-Coder-7B-Instruct",
        specialist_id=specialist_id,
        domain="code",
        difficulty_tiers=["medium"],
        required_backend="ollama",
        ollama_tag=ollama_tag,
        storage_gb=5.0,
        runtime_gb=6.0,
        min_vram_gb=8.0,
        context_window=32768,
        n_layers=28,
        estimated_tps_at={"gb10": 40.0},
    )


def test_discovery_to_snapshot_empty_returns_empty_snapshot():
    snap = discovery_to_snapshot(DiscoveryResult())
    assert snap.specialists == {}
    assert snap.nodes == {}
    assert snap.catalog == {}


def test_discovery_to_snapshot_synthesizes_bindings_per_node_url():
    """Each node_url in a discovered specialist becomes one NodeBinding."""
    spec = DiscoveredSpecialist(
        specialist_id="qwen2.5-coder-7b-q4-ollama",
        model_id="Qwen/Qwen2.5-Coder-7B-Instruct",
        domain="code",
        node_urls=(
            "http://192.168.1.10:11434",
            "http://192.168.1.20:11434",
        ),
    )
    result = DiscoveryResult(
        specialists={spec.specialist_id: spec},
        reachable=["192.168.1.10", "192.168.1.20"],
    )
    snap = discovery_to_snapshot(result)
    bindings = snap.specialists[spec.specialist_id]
    assert len(bindings) == 2
    assert {b.node_url for b in bindings} == {
        "http://192.168.1.10:11434",
        "http://192.168.1.20:11434",
    }
    # Node ids must be deterministic + human-meaningful (host:port).
    assert {b.node_id for b in bindings} == {"192.168.1.10:11434", "192.168.1.20:11434"}
    for b in bindings:
        assert b.health == "healthy"  # we just successfully fetched /models
        assert b.queue_depth == 0
        assert b.p95_latency_ms_60s is None


def test_discovery_to_snapshot_carries_local_catalog_for_rewrite():
    """The local catalog is what gives the router an `ollama_tag` to
    rewrite `model` against — otherwise `model = specialist_id` flows
    through unchanged.
    """
    spec = DiscoveredSpecialist(
        specialist_id="qwen2.5-coder-7b-q4-ollama",
        node_urls=("http://10.0.0.5:11434",),
    )
    result = DiscoveryResult(specialists={spec.specialist_id: spec})
    catalog = [_card("qwen2.5-coder-7b-q4-ollama")]
    snap = discovery_to_snapshot(result, catalog=catalog)
    assert "qwen2.5-coder-7b-q4-ollama" in snap.catalog
    assert snap.catalog["qwen2.5-coder-7b-q4-ollama"].ollama_tag == "qwen2.5-coder:7b"


def test_discovery_to_snapshot_keeps_node_summary_per_node():
    """One NodeSummary per derived node_id, even when several specialists
    share the same node URL."""
    spec_a = DiscoveredSpecialist(specialist_id="a", node_urls=("http://h:11434",))
    spec_b = DiscoveredSpecialist(specialist_id="b", node_urls=("http://h:11434",))
    result = DiscoveryResult(specialists={"a": spec_a, "b": spec_b})
    snap = discovery_to_snapshot(result)
    assert list(snap.nodes) == ["h:11434"]
    assert snap.nodes["h:11434"].health == "healthy"


# ---------------------------------------------------------------------------
# _RefreshingSnapshot
# ---------------------------------------------------------------------------


def test_refreshing_snapshot_cold_get_runs_refresher_synchronously():
    """First .get() before background loop runs must still return data."""
    calls = []

    def refresher() -> DiscoveryResult:
        calls.append(1)
        return DiscoveryResult(
            specialists={"x": DiscoveredSpecialist(specialist_id="x", node_urls=("http://h:80",))}
        )

    holder = _RefreshingSnapshot(refresher, refresh_s=60.0)  # tall interval — no auto-refresh
    snap = holder.get()
    assert "x" in snap.specialists
    assert len(calls) == 1


def test_refreshing_snapshot_caches_after_first_get():
    """Subsequent .get() before the next refresh tick returns the cached
    snapshot — does NOT call the refresher again."""
    calls = []

    def refresher() -> DiscoveryResult:
        calls.append(1)
        return DiscoveryResult()

    holder = _RefreshingSnapshot(refresher, refresh_s=60.0)
    holder.get()
    holder.get()
    holder.get()
    assert len(calls) == 1


def test_refreshing_snapshot_background_loop_swaps_snapshot():
    """Start the loop; the snapshot the router sees must reflect new
    discovery results without anyone calling .get() again."""
    state = {"version": 0}
    ready = threading.Event()

    def refresher() -> DiscoveryResult:
        state["version"] += 1
        sid = f"spec-v{state['version']}"
        if state["version"] >= 2:
            ready.set()
        return DiscoveryResult(
            specialists={sid: DiscoveredSpecialist(specialist_id=sid, node_urls=("http://h:80",))}
        )

    holder = _RefreshingSnapshot(refresher, refresh_s=0.05)
    holder.start()
    try:
        assert ready.wait(timeout=2.0), "refresher loop never reached version 2"
        snap = holder.get()
        assert any(sid.startswith("spec-v") for sid in snap.specialists)
    finally:
        holder.stop()


def test_refreshing_snapshot_swallows_refresher_exceptions():
    """A flaky refresh must not crash the daemon thread — the router has
    to keep serving the most recent snapshot it has."""
    state = {"first": True}

    def refresher() -> DiscoveryResult:
        if state["first"]:
            state["first"] = False
            return DiscoveryResult(
                specialists={
                    "good": DiscoveredSpecialist(specialist_id="good", node_urls=("http://h:80",))
                }
            )
        raise RuntimeError("transient discovery failure")

    holder = _RefreshingSnapshot(refresher, refresh_s=0.02)
    snap0 = holder.get()
    assert "good" in snap0.specialists
    holder.start()
    # Let the background loop fail a couple of times.
    time.sleep(0.15)
    holder.stop()
    # The snapshot from the cold .get() must still be present — failures
    # didn't overwrite it.
    snap1 = holder.get()
    assert "good" in snap1.specialists


def test_refreshing_snapshot_stop_terminates_thread():
    """Hygienic shutdown for `slancha-mesh router` SIGINT handling."""
    holder = _RefreshingSnapshot(lambda: DiscoveryResult(), refresh_s=0.05)
    holder.start()
    holder.stop(timeout=1.0)
    assert holder._thread is not None
    assert not holder._thread.is_alive()


# ---------------------------------------------------------------------------
# _RefreshingSnapshot — staleness-tolerant refresh (a transient discovery miss
# must not 404 a live route for a whole cycle, but a truly-gone node must
# eventually age out).
# ---------------------------------------------------------------------------


def _snap_with(sid: str, url: str, last_seen: datetime, ts: datetime) -> RegistrySnapshot:
    binding = NodeBinding(
        node_id=url, specialist_id=sid, health="healthy",
        queue_depth=0, node_url=url, last_seen=last_seen,
    )
    return RegistrySnapshot(
        snapshot_ts=ts,
        nodes={url: NodeSummary(
            node_id=url, friendly_name=url, health="healthy",
            last_seen=last_seen, node_url=url,
        )},
        specialists={sid: [binding]},
    )


def _holder(retain_s: float) -> _RefreshingSnapshot:
    return _RefreshingSnapshot(lambda: DiscoveryResult(), refresh_s=30.0, retain_s=retain_s)


def test_merge_retains_binding_a_pass_transiently_missed():
    """qwen3 was live last pass; this pass misses it (peer mid-restart). Within
    the retain window it stays routable instead of 404ing."""
    t0 = datetime(2026, 7, 14, tzinfo=timezone.utc)
    prior = _snap_with("qwen3", "http://gb10:11434", t0, t0)
    fresh = RegistrySnapshot(snapshot_ts=t0 + timedelta(seconds=30))  # empty pass, +30s
    merged = _holder(retain_s=60.0)._merge_retaining_recent(prior, fresh)
    assert "qwen3" in merged.specialists  # age 30s < retain 60s → retained
    assert merged.specialists["qwen3"][0].node_url == "http://gb10:11434"
    assert "http://gb10:11434" in {n.node_url for n in merged.nodes.values()}  # node carried too


def test_merge_ages_out_binding_past_retain_window():
    """A node that stays missing past retain_s stops being served — retention
    is a bridge, not a permanent zombie route."""
    t0 = datetime(2026, 7, 14, tzinfo=timezone.utc)
    prior = _snap_with("qwen3", "http://gb10:11434", t0, t0)
    fresh = RegistrySnapshot(snapshot_ts=t0 + timedelta(seconds=90))  # +90s > retain 60s
    merged = _holder(retain_s=60.0)._merge_retaining_recent(prior, fresh)
    assert "qwen3" not in merged.specialists  # aged out


def test_merge_fresh_binding_is_authoritative_not_duplicated():
    """When this pass re-sees the binding, fresh wins and it isn't double-added."""
    t0 = datetime(2026, 7, 14, tzinfo=timezone.utc)
    prior = _snap_with("qwen3", "http://gb10:11434", t0, t0)
    fresh_ts = t0 + timedelta(seconds=30)
    fresh = _snap_with("qwen3", "http://gb10:11434", fresh_ts, fresh_ts)
    merged = _holder(retain_s=60.0)._merge_retaining_recent(prior, fresh)
    assert len(merged.specialists["qwen3"]) == 1
    assert merged.specialists["qwen3"][0].last_seen == fresh_ts  # the fresh binding


def test_merge_retain_zero_restores_strict_drop():
    """SLANCHA_ROUTER_BINDING_RETAIN_S=0 → old behaviour: a missed pass drops
    the binding immediately (fresh returned unchanged)."""
    t0 = datetime(2026, 7, 14, tzinfo=timezone.utc)
    prior = _snap_with("qwen3", "http://gb10:11434", t0, t0)
    fresh = RegistrySnapshot(snapshot_ts=t0 + timedelta(seconds=1))
    merged = _holder(retain_s=0.0)._merge_retaining_recent(prior, fresh)
    assert "qwen3" not in merged.specialists
    assert merged is fresh
