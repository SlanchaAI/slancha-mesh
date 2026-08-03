"""Request-outcome health: metadata reachability is not inference readiness."""

from __future__ import annotations

from mesh.runtime_health import RouterRuntimeHealth, binding_key


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_failures_open_circuit_then_successful_half_open_probe_closes_it():
    clock = FakeClock()
    health = RouterRuntimeHealth(
        failure_threshold=2,
        cooldown_s=30.0,
        clock=clock,
    )
    key = binding_key("code", "spark:11434")

    health.record_failure(key, "timeout")
    assert health.is_routable(key)
    health.record_failure(key, "timeout")
    assert not health.is_routable(key)

    clock.advance(30.0)
    assert health.is_routable(key)
    assert not health.is_routable(key)  # only one half-open request at a time
    health.record_success(key, latency_ms=420)

    assert health.is_routable(key)
    assert health.snapshot()["open_circuits"] == 0


def test_failed_half_open_probe_restarts_full_cooldown():
    clock = FakeClock()
    health = RouterRuntimeHealth(failure_threshold=1, cooldown_s=5, clock=clock)
    key = binding_key("code", "spark:11434")

    health.record_failure(key, "http_503")
    clock.advance(5)
    assert health.is_routable(key)
    health.record_failure(key, "http_503")

    assert not health.is_routable(key)
    clock.advance(4.9)
    assert not health.is_routable(key)
    clock.advance(0.1)
    assert health.is_routable(key)


def test_client_errors_do_not_penalize_node_and_counters_are_visible():
    health = RouterRuntimeHealth(failure_threshold=1, cooldown_s=5)
    key = binding_key("code", "spark:11434")

    health.record_client_error()
    health.record_punt()

    assert health.is_routable(key)
    assert health.snapshot()["client_errors"] == 1
    assert health.snapshot()["punts"] == 1


def test_snapshot_is_bounded_and_never_contains_request_content():
    health = RouterRuntimeHealth(
        failure_threshold=1,
        cooldown_s=5,
        max_bindings=2,
    )
    health.record_failure(binding_key("a", "one"), "x" * 500)
    health.record_success(binding_key("b", "two"), latency_ms=25)
    health.record_success(binding_key("c", "three"), latency_ms=30)

    snapshot = health.snapshot()
    assert len(snapshot["bindings"]) == 2
    assert snapshot["requests_succeeded"] == 2
    assert snapshot["requests_failed"] == 1
    rendered = repr(snapshot).lower()
    assert "prompt" not in rendered
    assert "completion" not in rendered
    for state in snapshot["bindings"].values():
        assert len(state["last_failure_class"]) <= 64


def test_peek_routable_does_not_consume_half_open_probe():
    clock = FakeClock()
    health = RouterRuntimeHealth(failure_threshold=1, cooldown_s=5, clock=clock)
    key = binding_key("code", "spark:11434")
    health.record_failure(key, "timeout")
    clock.advance(5)

    assert health.peek_routable(key)
    assert health.peek_routable(key)
    assert health.is_routable(key)
    assert not health.is_routable(key)
