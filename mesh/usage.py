"""Neutral usage-telemetry seam for the mesh router.

On a completed inference the router hands a neutral usage event (token counts +
metadata only — NEVER prompt/completion bodies) to an injected `UsageSink`. Default
is `NullSink` (no-op → telemetry off, zero behavior change out of the box). The real
impl, `SpoolDrainSink`, appends events to a local append-only JSONL spool (off the
network path — never blocks or faults a completion) and a background asyncio task
drains them by POSTing **one event per request** to a configured receiver, with
retry/backoff, poison-row isolation, and a bounded spool.

DESIGN BOUNDARY (Apache-2.0 OSS): this module is metering-NEUTRAL. It carries NO
downstream-consumer-specific logic — no pricing, no actor policy, no receiver-shaped
payload beyond an env-configured endpoint. A consumer prices + attributes on its side;
mesh only counts.

Single-writer-per-file: the spool append + rewrite assume ONE process owns the file
(a per-instance lock makes it thread-safe within the process). mesh runs single-process
today (`cli.py` uvicorn, no `workers=`). If multi-worker is ever adopted, add `flock`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

_log = logging.getLogger("mesh.usage")

# --- bounds / defaults ----------------------------------------------------
MAX_ROW_BYTES = 64 * 1024          # a serialized event over this is dropped (never a torn line)
MAX_TOKENS = 100_000_000           # sane ceiling; above this is not a real count
MAX_REQUEST_ID = 128               # canonical schema maxLength
MAX_USER_ID_BYTES = 256            # schema has no max; cap to bound spool/ledger DoS
MAX_TAIL_BYTES = 64 * 1024         # streaming tail buffer bound
BATCH_MAX = 500                    # lines drained per pass (bounds memory + per-pass latency)
DEFAULT_DRAIN_INTERVAL_S = 5.0
MAX_BACKOFF_S = 60.0
SOFT_CAP_BYTES = 8 * 1024 * 1024   # warn above this
HARD_CAP_BYTES = 32 * 1024 * 1024  # drop oldest down to ~50% above this

_ENDPOINT = "/v1/chat/completions"


# --------------------------------------------------------------------------
# Pure helpers (no I/O) — exhaustively unit-testable
# --------------------------------------------------------------------------
def _canonical(row: dict[str, Any]) -> str:
    """Deterministic serialization: sorted keys, compact, UTF-8 preserved. json.dumps
    escapes control chars, so an embedded newline can never inject a phantom spool line."""
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_count(x: Any) -> int | None:
    """A token count is trustworthy only if it is a non-bool int in [0, MAX_TOKENS].
    Rejects negative / float / str / bool / absurd / None — the ledger-poisoning vectors.
    (`isinstance(True, int)` is True, so bool must be rejected explicitly.)"""
    if isinstance(x, bool):
        return None
    if not isinstance(x, int):
        return None
    if x < 0 or x > MAX_TOKENS:
        return None
    return x


def _clean_user(x: Any) -> str | None:
    """NFKC-normalize + validate a caller-asserted user id. None when unusable
    (non-str, empty/`none`/`null`, or over the byte cap — an 8MB `body.user` is not a
    real user id, it's a DoS primitive). Mirrors audit.py `_clean_actor`."""
    if not isinstance(x, str):
        return None
    s = unicodedata.normalize("NFKC", x).strip()
    if not s or s.lower() in {"none", "null"}:
        return None
    if len(s.encode("utf-8")) > MAX_USER_ID_BYTES:
        return None
    return s


def _clean_request_id(x: Any) -> str | None:
    """Use the upstream response `id` when it's a sane string (capped to the schema max);
    else None so the caller stamps a uuid."""
    if not isinstance(x, str):
        return None
    s = x.strip()[:MAX_REQUEST_ID]
    return s or None


def parse_response_body(content: Any) -> dict[str, Any]:
    """Guarded parse of an upstream response body. NEVER raises — a malformed / non-dict /
    hostile body yields `{}` so the telemetry tap can never fault a client's completion."""
    try:
        obj = json.loads(content)
    except Exception:  # noqa: BLE001 — any parse failure is just "no usable metadata"
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_stream_usage(tail: bytes) -> dict[str, Any] | None:
    """Extract the `usage` block from the tail of an OpenAI SSE stream. Present only when
    the caller set `stream_options.include_usage`. Returns the LAST `data:` line's usage
    dict, or None. Fully guarded."""
    try:
        text = tail.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    found: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:  # noqa: BLE001
            continue
        u = obj.get("usage") if isinstance(obj, dict) else None
        if isinstance(u, dict):
            found = u
    return found


def build_usage_event(
    *,
    specialist_id: str,
    user_field: Any,
    status_code: int,
    latency_ms: int,
    usage: Any,
    response_id: Any = None,
    ttft_ms: int | None = None,
    fallback_fired: bool = False,
) -> dict[str, Any] | None:
    """Assemble the §6 wire event (v1.0.0 shape) from what the router observed.

    Returns None (caller SKIPS the emit) when token counts are absent or untrustworthy —
    we never inject a fabricated 0/garbage count into a cost ledger. Omits cost fields
    (mesh has no pricing; the consumer prices from model+tokens) and `ts` (deferred to
    the coordinated wire bump).
    """
    if not isinstance(usage, dict):
        return None
    tin = _safe_count(usage.get("prompt_tokens"))
    tout = _safe_count(usage.get("completion_tokens"))
    if tin is None or tout is None:
        return None
    user_id = _clean_user(user_field) or "unattributed"
    request_id = _clean_request_id(response_id) or ("req-" + uuid.uuid4().hex)
    latency_ms = max(0, int(latency_ms))
    ev: dict[str, Any] = {
        "request_id": request_id,
        "user_id": user_id,
        "endpoint": _ENDPOINT,
        "model": specialist_id,
        "route": "mesh",
        "tokens_in": tin,
        "tokens_out": tout,
        "latency_ms": latency_ms,
        "status_code": int(status_code),
        "specialist_id": specialist_id,
        "fallback_fired": bool(fallback_fired),
        # OTel dotted aliases — part of the wire contract (H19).
        "gen_ai.request.model": specialist_id,
        "gen_ai.usage.input_tokens": tin,
        "gen_ai.usage.output_tokens": tout,
    }
    if ttft_ms is not None:
        ev["ttft_ms"] = max(0, int(ttft_ms))
    if latency_ms and tout:
        ev["tokens_per_second"] = round(tout / (latency_ms / 1000.0), 2)
    return ev


# --------------------------------------------------------------------------
# Sink interface + the no-op default
# --------------------------------------------------------------------------
class UsageSink(Protocol):
    """Downstream sink for usage events. One method; called synchronously on the
    completion path, so it MUST be a fast local op (spool + async drain, never a
    blocking network call)."""

    def emit(self, event: dict[str, Any]) -> None: ...


class NullSink:
    """Default sink — drops events. Telemetry is off unless a real sink is injected."""

    def emit(self, event: dict[str, Any]) -> None:  # noqa: D401
        return None


def _bump(sink: Any, name: str) -> None:
    """Increment a named counter on the sink iff it exposes one (SpoolDrainSink does;
    NullSink doesn't — no-op there)."""
    if hasattr(sink, name):
        try:
            setattr(sink, name, getattr(sink, name) + 1)
        except Exception:  # noqa: BLE001
            pass


def safe_emit(sink: Any, event: dict[str, Any] | None) -> None:
    """Emit an event, or count the skip when the builder returned None. Telemetry NEVER
    faults a completion — every failure is swallowed + logged."""
    if event is None:
        _log.warning("usage event skipped (no usable token counts)")
        _bump(sink, "skipped_missing")
        return
    try:
        sink.emit(event)
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a completion
        _log.warning("usage sink.emit failed (%s: %s); event dropped", type(exc).__name__, exc)


# --------------------------------------------------------------------------
# Spool + drain sink
# --------------------------------------------------------------------------
@dataclass
class DrainResult:
    delivered: int = 0
    poison: int = 0
    corrupt: int = 0
    posted: int = 0
    stopped_transient: bool = False


class SpoolDrainSink:
    """At-least-once local JSONL spool with a background drain.

    `emit` (sync) appends one line — off the network path, never blocks a completion.
    `run` (async) drains: one `POST /v1/usage` per line (matching the receiver's real
    single-event contract), 2xx → delivered, 4xx → poison (drop, don't wedge the FIFO),
    5xx/transport/corrupt-line → stop-and-retry-next-pass. Redelivery is safe because the
    event carries a stable `request_id` the receiver dedups on.
    """

    def __init__(
        self,
        spool: str | os.PathLike[str],
        receiver_url: str,
        *,
        token: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        interval_s: float = DEFAULT_DRAIN_INTERVAL_S,
        batch_max: int = BATCH_MAX,
        soft_cap_bytes: int = SOFT_CAP_BYTES,
        hard_cap_bytes: int = HARD_CAP_BYTES,
        timeout_s: float = 10.0,
    ) -> None:
        self.spool = Path(spool)
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        self._url = receiver_url
        self._token = token
        self._client = http_client
        self._owns_client = http_client is None
        self._timeout = timeout_s
        self._interval = interval_s
        self._batch_max = batch_max
        self._soft = soft_cap_bytes
        self._hard = hard_cap_bytes
        self._lock = threading.Lock()
        self._stopped = False
        self._task: asyncio.Task[Any] | None = None
        self._backoff = 0.0
        # in-process counters (asserted in tests + logged)
        self.spooled = 0
        self.skipped_missing = 0
        self.poison_dropped = 0
        self.overflow_dropped = 0
        self.corrupt_lines = 0
        self.oversized_dropped = 0

    # --- sync emit (completion path) --------------------------------------
    def emit(self, event: dict[str, Any]) -> None:
        line = _canonical(event)
        if "\n" in line or "\r" in line:  # defense; json.dumps already escapes these
            _log.warning("usage event contains a newline; dropping")
            self.oversized_dropped += 1
            return
        data = (line + "\n").encode("utf-8")
        if len(data) > MAX_ROW_BYTES:
            _log.warning("usage event %d bytes exceeds MAX_ROW_BYTES; dropping", len(data))
            self.oversized_dropped += 1
            return
        with self._lock:
            self._enforce_cap_locked(len(data))
            # Open fresh each call (no cached fd → no stale-inode write after a rewrite);
            # O_APPEND + a single os.write is atomic under the lock. Mode 0o600 on create
            # (the spool holds user_ids + counts). No fsync — the design is duplicate-
            # tolerant and wants speed; do NOT "harden" this into an fsync later.
            fd = os.open(self.spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            self.spooled += 1

    def _enforce_cap_locked(self, incoming: int) -> None:
        try:
            size = self.spool.stat().st_size  # cheap; NOT a line scan
        except FileNotFoundError:
            return
        if size + incoming <= self._hard:
            if size >= self._soft:
                _log.warning("usage spool %d bytes over soft cap %d", size, self._soft)
            return
        # Hard cap: drop OLDEST down to ~50% in ONE rewrite (hysteresis — never per-line).
        target = self._hard // 2
        lines = self._read_lines_locked()
        kept: list[str] = []
        running = 0
        for ln in reversed(lines):  # keep the NEWEST up to target
            b = len(ln.encode("utf-8")) + 1
            if running + b > target:
                break
            running += b
            kept.append(ln)
        kept.reverse()
        dropped = len(lines) - len(kept)
        self._rewrite_locked(kept)
        self.overflow_dropped += dropped
        _log.warning("usage spool overflow: dropped %d oldest events (kept %d)", dropped, len(kept))

    # --- spool file ops (all under the caller's lock) ---------------------
    def _read_lines_locked(self) -> list[str]:
        if not self.spool.exists():
            return []
        with self.spool.open("r", encoding="utf-8") as f:
            return [ln.rstrip("\n") for ln in f if ln.strip()]

    def _rewrite_locked(self, lines: list[str]) -> None:
        tmp = self.spool.with_name(self.spool.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
            f.flush()
        os.replace(tmp, self.spool)  # atomic — a crash mid-rewrite can't lose the tail
        try:
            os.chmod(self.spool, 0o600)
        except OSError:
            pass

    # --- async drain ------------------------------------------------------
    async def drain_once(self) -> DrainResult:
        res = DrainResult()
        with self._lock:  # snapshot the head batch; lock released BEFORE any await
            lines = self._read_lines_locked()
        if not lines:
            return res
        batch = lines[: self._batch_max]
        client = self._client_or_build()
        processed = 0  # contiguous head run consumed (delivered ∪ poison ∪ corrupt)
        for raw in batch:
            try:
                event = json.loads(raw)
            except Exception:  # noqa: BLE001 — a corrupt line must not wedge the loop
                self.corrupt_lines += 1
                res.corrupt += 1
                processed += 1
                continue
            verdict = await self._post_one(client, event)  # network await — NO lock held
            res.posted += 1
            if verdict == "delivered":
                res.delivered += 1
                processed += 1
            elif verdict == "poison":
                self.poison_dropped += 1
                res.poison += 1
                processed += 1
            else:  # transient — stop; keep the remainder for the next pass
                res.stopped_transient = True
                break
        if processed:
            with self._lock:
                current = self._read_lines_locked()  # may have grown (appends at end only)
                self._rewrite_locked(current[processed:])  # drop the stable head prefix
        return res

    async def _post_one(self, client: httpx.AsyncClient, event: dict[str, Any]) -> str:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = await client.post(self._url, json=event, headers=headers, timeout=self._timeout)
        except (httpx.HTTPError, OSError):
            return "transient"  # never log the exception repr (can carry the token header)
        code = resp.status_code
        if 200 <= code < 300:
            return "delivered"
        if 400 <= code < 500:
            _log.warning("usage receiver rejected an event (status %d); dropping as poison", code)
            return "poison"
        return "transient"  # 5xx → retry next pass

    def _client_or_build(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    # --- lifecycle --------------------------------------------------------
    async def run(self) -> None:
        while not self._stopped:
            try:
                res = await self.drain_once()
                self._backoff = min(MAX_BACKOFF_S, (self._backoff or self._interval) * 2) \
                    if res.stopped_transient else 0.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a dead silent loop is the failure mode
                _log.warning("usage-drain-loop-error (%s: %s); continuing", type(exc).__name__, exc)
                self._backoff = min(MAX_BACKOFF_S, (self._backoff or self._interval) * 2)
            await asyncio.sleep(self._backoff or self._interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.run())

    async def aclose(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._owns_client and self._client is not None:
            await self._client.aclose()


__all__ = [
    "UsageSink",
    "NullSink",
    "SpoolDrainSink",
    "DrainResult",
    "build_usage_event",
    "parse_response_body",
    "parse_stream_usage",
    "safe_emit",
]
