"""Missing eviction and concurrent reservation contracts for runtime health."""

from concurrent.futures import Future
from threading import Barrier, Thread

from mesh.runtime_health import RouterRuntimeHealth


def test_eviction_prefers_newer_closed_binding_over_older_open_binding():
    now = [100.0]
    health = RouterRuntimeHealth(
        failure_threshold=1, max_bindings=2, clock=lambda: now[0]
    )
    health.record_failure("open", "timeout")
    now[0] += 1
    health.record_success("closed", 10)
    now[0] += 1

    assert health.is_routable("new")

    bindings = health.snapshot()["bindings"]
    assert set(bindings) == {"open", "new"}
    assert bindings["open"]["state"] == "open"
    assert not health.is_routable("open")


def test_equal_age_eviction_uses_key_order_not_insertion_order():
    health = RouterRuntimeHealth(max_bindings=2, clock=lambda: 100.0)
    health.record_success("z-first", 10)
    health.record_success("a-second", 10)

    assert health.is_routable("new")

    assert set(health.snapshot()["bindings"]) == {"z-first", "new"}


def test_concurrent_callers_reserve_only_one_probe_at_exact_cooldown():
    now = [100.0]
    health = RouterRuntimeHealth(
        failure_threshold=1, cooldown_s=5, clock=lambda: now[0]
    )
    health.record_failure("binding", "timeout")
    now[0] += 5
    callers = 8
    start = Barrier(callers)
    results = [Future() for _ in range(callers)]

    def reserve(result):
        try:
            start.wait(timeout=5)
            result.set_result(health.is_routable("binding"))
        except BaseException as exc:
            result.set_exception(exc)

    threads = [Thread(target=reserve, args=(result,), daemon=True) for result in results]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert [result.result(timeout=0) for result in results].count(True) == 1
    assert health.snapshot()["bindings"]["binding"]["state"] == "half_open"
    assert not health.peek_routable("binding")
    assert not health.is_routable("binding")
    health.record_success("binding", 10)
    assert health.is_routable("binding")
