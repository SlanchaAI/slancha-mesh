"""Bounded passive health for router-to-node request bindings."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


def binding_key(specialist_id: str, node_id: str) -> str:
    """Return the stable runtime-health key for one specialist/node binding."""

    return f"{specialist_id}@{node_id}"


@dataclass
class _BindingState:
    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_touched: float = 0.0
    last_success_at: float | None = None
    last_latency_ms: int | None = None
    last_failure_class: str = ""


class RouterRuntimeHealth:
    """Thread-safe circuit state derived from completed inference attempts."""

    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        cooldown_s: float = 30.0,
        max_bindings: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        if max_bindings < 1:
            raise ValueError("max_bindings must be at least 1")
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._max_bindings = max_bindings
        self._clock = clock
        self._lock = threading.Lock()
        self._bindings: dict[str, _BindingState] = {}
        self._requests_succeeded = 0
        self._requests_failed = 0
        self._client_errors = 0
        self._punts = 0

    def _state_for(self, key: str, now: float) -> _BindingState:
        state = self._bindings.get(key)
        if state is not None:
            state.last_touched = now
            return state
        if len(self._bindings) >= self._max_bindings:
            candidates = [
                (known.last_touched, known_key)
                for known_key, known in self._bindings.items()
                if known.state == "closed"
            ]
            if not candidates:
                candidates = [
                    (known.last_touched, known_key)
                    for known_key, known in self._bindings.items()
                ]
            _, evicted = min(candidates)
            del self._bindings[evicted]
        state = _BindingState(last_touched=now)
        self._bindings[key] = state
        return state

    def is_routable(self, key: str) -> bool:
        """Reserve a route, allowing only one probe after circuit cooldown."""

        now = self._clock()
        with self._lock:
            state = self._state_for(key, now)
            if state.state == "closed":
                return True
            if state.state == "half_open":
                return False
            if state.opened_at is None or now - state.opened_at < self._cooldown_s:
                return False
            state.state = "half_open"
            return True

    def peek_routable(self, key: str) -> bool:
        """Report availability without reserving a half-open probe."""

        now = self._clock()
        with self._lock:
            state = self._bindings.get(key)
            if state is None or state.state == "closed":
                return True
            if state.state == "half_open":
                return False
            return state.opened_at is not None and now - state.opened_at >= self._cooldown_s

    def record_success(self, key: str, latency_ms: int) -> None:
        now = self._clock()
        with self._lock:
            state = self._state_for(key, now)
            state.state = "closed"
            state.consecutive_failures = 0
            state.opened_at = None
            state.last_success_at = now
            state.last_latency_ms = max(0, latency_ms)
            state.last_failure_class = ""
            self._requests_succeeded += 1

    def record_failure(self, key: str, failure_class: str) -> None:
        now = self._clock()
        with self._lock:
            state = self._state_for(key, now)
            was_probe = state.state == "half_open"
            state.consecutive_failures += 1
            state.last_failure_class = failure_class[:64]
            if was_probe or state.consecutive_failures >= self._failure_threshold:
                state.state = "open"
                state.opened_at = now
            self._requests_failed += 1

    def record_client_error(self) -> None:
        with self._lock:
            self._client_errors += 1

    def record_punt(self) -> None:
        with self._lock:
            self._punts += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            bindings = {
                key: {
                    "state": state.state,
                    "consecutive_failures": state.consecutive_failures,
                    "last_success_at": state.last_success_at,
                    "last_latency_ms": state.last_latency_ms,
                    "last_failure_class": state.last_failure_class,
                }
                for key, state in self._bindings.items()
            }
            return {
                "open_circuits": sum(
                    1 for state in self._bindings.values() if state.state != "closed"
                ),
                "requests_succeeded": self._requests_succeeded,
                "requests_failed": self._requests_failed,
                "client_errors": self._client_errors,
                "punts": self._punts,
                "bindings": bindings,
            }


__all__ = ["RouterRuntimeHealth", "binding_key"]

