# Truthful Routing, Escalation, and Health Implementation Plan

**Status:** Superseded on 2026-08-01 by
`2026-08-01-oss-routing-integration.md`. The original in-process cloud proxy
duplicated mature OSS gateway behavior and blurred Barkeep's spend boundary.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the core router distinguish discovery from request readiness, return a stable typed punt, and optionally proxy an explicitly authorized request to one configured OpenAI-compatible fallback.

**Architecture:** Merge the current LAN work with `origin/main`, then add two focused modules. `mesh.fallback` owns validated configuration and request permission; `mesh.runtime_health` owns bounded per-binding circuit state and counters. `mesh.router_app` orchestrates local attempts, escalation, and response headers, while `mesh.cli` maps explicit flags and environment variables into configuration.

**Tech Stack:** Python 3.11+, FastAPI, httpx, Pydantic models already present in `mesh.models`, pytest, Ruff, GitNexus.

## Global Constraints

- Preserve explicit specialist IDs, LAN discovery, tailnet discovery, and `specialists_reachable`.
- Preserve the usage-telemetry seam merged on `origin/main`.
- Default fallback mode is `punt`; no cloud request occurs without complete server configuration and request permission.
- Return typed punts with HTTP 503, `X-Slancha-Outcome: punt`, and `X-Slancha-Reason`.
- Never forward caller authorization to a node or fallback target.
- Bound request bodies, fallback attempts, circuit history, health output, and error text.
- Keep prompts and completion bodies out of logs and health state.
- Use one writer. Run GitNexus impact analysis before each symbol edit and `detect_changes --base-ref origin/main` before each commit.
- Do not push, merge to the default branch, publish, download model weights, or install a heavy engine.

---

## File Map

- Create `mesh/fallback.py`: validated fallback configuration, trigger enum, request-permission parser, sanitized headers, and typed-punt body builder.
- Create `mesh/runtime_health.py`: thread-safe circuit breaker and bounded router counters.
- Modify `mesh/router_app.py`: consume the two modules; record local outcomes; punt or proxy after unroutable/unavailable outcomes; expose richer health.
- Modify `mesh/cli.py`: parse fallback flags/env and inject `FallbackConfig` into the app.
- Create `mesh/tests/test_fallback.py`: pure configuration, URL, permission, and punt tests.
- Create `mesh/tests/test_runtime_health.py`: deterministic circuit and counter tests with an injected clock.
- Modify `mesh/tests/test_router_auto.py`: auto-route punt and fallback behavior.
- Modify `mesh/tests/test_router_app.py`: explicit-model exhaustion, streaming/non-streaming fallback, credential isolation, health, and recovery behavior.
- Modify `mesh/tests/test_cli.py`: parser and startup validation.
- Create `mesh/tests/test_router_live_socket.py`: real-socket router → local node/fallback proof without provider spend.

### Task 1: Integrate the current default branch

**Files:**
- Merge: `origin/main`
- Preserve: `mesh/cli.py`, `mesh/router_app.py`, `mesh/serve.py`, `mesh/tests/test_tailnet.py`, `mesh/catalog/qwen3-14b-q4-ollama.toml`

**Interfaces:**
- Consumes: current commit `435be4b`, LAN commits `ee11446` and `bd8b742`, remote default `origin/main`.
- Produces: one branch containing LAN advertise-host support, the Qwen card, and usage telemetry.

- [ ] **Step 1: Record the pre-merge graph and worktree**

Run:

```bash
git status --short --branch
git log --oneline --decorate --graph -12
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope compare --base-ref origin/main
```

Expected: only the user's existing `AGENTS.md` edit and two untracked files are unrelated; GitNexus reports the known HIGH divergence in router and LAN flows.

- [ ] **Step 2: Merge without rewriting history**

Run:

```bash
git merge --no-edit origin/main
```

Expected: a merge commit or a clean merge stop. Resolve only conflicts in files changed by the LAN and telemetry commits; preserve both behaviors.

- [ ] **Step 3: Rebuild the graph and run integration-focused tests**

Run:

```bash
node .gitnexus/run.cjs analyze
uv run python -m pytest -q \
  mesh/tests/test_tailnet.py \
  mesh/tests/test_router_app.py \
  mesh/tests/test_usage_emitter.py
```

Expected: all selected tests pass.

- [ ] **Step 4: Commit only if conflict resolution created an uncommitted merge**

Run:

```bash
git status --short
git commit -m "Merge current main into OSS core work"
```

Expected: the merge is recorded; user-owned files remain untracked or unstaged.

### Task 2: Define fallback configuration and the typed punt contract

**Files:**
- Create: `mesh/fallback.py`
- Create: `mesh/tests/test_fallback.py`

**Interfaces:**
- Consumes: `httpx.Timeout`, standard-library `dataclass`, `Enum`, `os`, and `urllib.parse.urlsplit`.
- Produces:
  - `FallbackMode(str, Enum)`: `PUNT`, `PROXY`.
  - `FallbackTrigger(str, Enum)`: `UNROUTABLE`, `UNAVAILABLE`, `BOTH`.
  - `FallbackPermission(str, Enum)`: `DENY`, `PROXY`.
  - `FallbackConfig.validate() -> None`.
  - `FallbackConfig.allows(trigger: FallbackTrigger, header: str | None) -> bool`.
  - `FallbackConfig.endpoint -> str`.
  - `FallbackConfig.timeout -> httpx.Timeout`.
  - `FallbackConfig.headers() -> dict[str, str]`.
  - `punt_payload(code: str, message: str, *, local_attempts: int, retryable: bool) -> dict`.

- [ ] **Step 1: Write failing pure tests**

Add tests that assert:

```python
def test_punt_is_the_zero_config_default():
    cfg = FallbackConfig()
    cfg.validate()
    assert cfg.mode is FallbackMode.PUNT
    assert not cfg.allows(FallbackTrigger.UNROUTABLE, None)


def test_proxy_requires_url_and_model():
    with pytest.raises(ValueError, match="fallback URL and model"):
        FallbackConfig(mode=FallbackMode.PROXY).validate()


def test_request_deny_overrides_proxy_default():
    cfg = _proxy(default_permission=FallbackPermission.PROXY)
    assert not cfg.allows(FallbackTrigger.UNAVAILABLE, "deny")


def test_request_proxy_enables_deny_default():
    cfg = _proxy(default_permission=FallbackPermission.DENY)
    assert cfg.allows(FallbackTrigger.UNAVAILABLE, "proxy")


def test_http_fallback_rejects_public_dns():
    with pytest.raises(ValueError, match="HTTPS"):
        _proxy(url="http://api.example.com/v1/chat/completions").validate()


def test_https_fallback_rejects_userinfo_query_and_fragment():
    for url in (
        "https://user:pass@api.example.com/v1/chat/completions",
        "https://api.example.com/v1/chat/completions?key=x",
        "https://api.example.com/v1/chat/completions#frag",
    ):
        with pytest.raises(ValueError):
            _proxy(url=url).validate()


def test_headers_read_only_the_named_environment_variable(monkeypatch):
    monkeypatch.setenv("TEST_FALLBACK_KEY", "secret")
    cfg = _proxy(api_key_env="TEST_FALLBACK_KEY")
    assert cfg.headers() == {
        "Content-Type": "application/json",
        "Authorization": "Bearer secret",
    }
```

- [ ] **Step 2: Run tests and confirm the missing module failure**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_fallback.py
```

Expected: collection fails because `mesh.fallback` does not exist.

- [ ] **Step 3: Implement the minimal module**

Use this public shape:

```python
@dataclass(frozen=True)
class FallbackConfig:
    mode: FallbackMode = FallbackMode.PUNT
    url: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    trigger: FallbackTrigger = FallbackTrigger.BOTH
    default_permission: FallbackPermission = FallbackPermission.DENY
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 120.0

    def validate(self) -> None:
        """Raise ValueError when the configured target is unsafe or incomplete."""
        raise NotImplementedError

    def allows(self, trigger: FallbackTrigger, header: str | None) -> bool:
        """Return whether server and request gates authorize this trigger."""
        raise NotImplementedError

    @property
    def endpoint(self) -> str:
        """Return the validated chat-completions URL."""
        raise NotImplementedError

    @property
    def timeout(self) -> httpx.Timeout:
        """Return the configured connect/read timeout with bounded write/pool values."""
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        """Build isolated fallback headers from the named environment variable."""
        raise NotImplementedError
```

Validation rules:

- `proxy` requires a non-empty URL and model.
- URL scheme is HTTPS, except `http://localhost`, `http://127.0.0.0/8`,
  `http://[::1]`, and literal private/CGNAT IP addresses.
- Reject URL userinfo, query, fragment, missing host, non-positive timeouts,
  and newline-bearing model or environment-variable names.
- `headers()` raises a clear startup/configuration error when a named key
  variable is absent or empty. It returns no authorization header when
  `api_key_env` is `None`.
- Unknown request permission values raise `ValueError`; `deny` always wins.

- [ ] **Step 4: Run focused tests and Ruff**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_fallback.py
uv run ruff check mesh/fallback.py mesh/tests/test_fallback.py
```

Expected: both commands pass.

- [ ] **Step 5: Review graph changes and commit**

Run:

```bash
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope unstaged
git add mesh/fallback.py mesh/tests/test_fallback.py
git commit -m "Add explicit fallback and punt contract"
```

Expected: LOW risk; only the new module and tests are committed.

### Task 3: Add bounded runtime circuit state

**Files:**
- Create: `mesh/runtime_health.py`
- Create: `mesh/tests/test_runtime_health.py`

**Interfaces:**
- Consumes: a binding key formatted as `"<specialist_id>@<node_id>"` and an injected monotonic clock.
- Produces:
  - `binding_key(specialist_id: str, node_id: str) -> str`.
  - `RouterRuntimeHealth.is_routable(key: str) -> bool`.
  - `RouterRuntimeHealth.record_success(key: str, latency_ms: int) -> None`.
  - `RouterRuntimeHealth.record_failure(key: str, failure_class: str) -> None`.
  - `RouterRuntimeHealth.record_client_error() -> None`.
  - `RouterRuntimeHealth.record_punt() -> None`.
  - `RouterRuntimeHealth.record_cloud_fallback(success: bool) -> None`.
  - `RouterRuntimeHealth.snapshot() -> dict[str, object]`.

- [ ] **Step 1: Write failing deterministic tests**

Cover these transitions with a fake clock:

```python
def test_two_retriable_failures_open_then_successful_half_open_closes():
    clock = FakeClock()
    health = RouterRuntimeHealth(
        failure_threshold=2, cooldown_s=30.0, clock=clock,
    )
    key = binding_key("code", "spark:11434")
    health.record_failure(key, "timeout")
    assert health.is_routable(key)
    health.record_failure(key, "timeout")
    assert not health.is_routable(key)
    clock.advance(30.0)
    assert health.is_routable(key)
    health.record_success(key, 420)
    assert health.snapshot()["open_circuits"] == 0


def test_half_open_allows_one_probe_until_it_finishes():
    clock = FakeClock()
    health = RouterRuntimeHealth(failure_threshold=1, cooldown_s=5, clock=clock)
    key = binding_key("code", "spark:11434")
    health.record_failure(key, "timeout")
    clock.advance(5)
    assert health.is_routable(key)
    assert not health.is_routable(key)


def test_success_and_failure_counters_are_bounded():
    health = RouterRuntimeHealth(failure_threshold=2, cooldown_s=5)
    key = binding_key("code", "spark:11434")
    health.record_success(key, 50)
    health.record_failure(key, "x" * 500)
    snap = health.snapshot()
    assert snap["requests_succeeded"] == 1
    assert len(snap["bindings"][key]["last_failure_class"]) == 64


def test_client_error_is_not_recorded_as_node_failure():
    health = RouterRuntimeHealth(failure_threshold=1, cooldown_s=5)
    key = binding_key("code", "spark:11434")
    health.record_client_error()
    assert health.is_routable(key)
    assert health.snapshot()["client_errors"] == 1


def test_snapshot_never_contains_prompt_or_response_fields():
    health = RouterRuntimeHealth(failure_threshold=1, cooldown_s=5)
    health.record_failure(binding_key("code", "spark:11434"), "timeout")
    rendered = json.dumps(health.snapshot()).lower()
    assert "prompt" not in rendered
    assert "completion" not in rendered
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_runtime_health.py
```

Expected: collection fails because `mesh.runtime_health` does not exist.

- [ ] **Step 3: Implement the state machine**

Use a `threading.Lock`, one `_BindingState` dataclass per key, a maximum of 256
binding entries, and aggregate integer counters. Evict the least-recently
touched closed entry when the cap is reached. Store failure classes capped at
64 characters. Do not store request content.

Half-open semantics:

- Before cooldown: `is_routable` returns false.
- At cooldown expiry: the first `is_routable` caller reserves the half-open
  probe and returns true; later callers return false.
- `record_success` closes and resets the circuit.
- `record_failure` reopens it from the current clock.

- [ ] **Step 4: Run tests and Ruff**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_runtime_health.py
uv run ruff check mesh/runtime_health.py mesh/tests/test_runtime_health.py
```

Expected: both commands pass.

- [ ] **Step 5: Review and commit**

Run:

```bash
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope unstaged
git add mesh/runtime_health.py mesh/tests/test_runtime_health.py
git commit -m "Track router request readiness with circuits"
```

Expected: LOW risk; new module only.

### Task 4: Wire typed punts and non-streaming proxy fallback

**Files:**
- Modify: `mesh/router_app.py`
- Modify: `mesh/tests/test_router_auto.py`
- Modify: `mesh/tests/test_router_app.py`

**Interfaces:**
- Consumes: `FallbackConfig`, `FallbackTrigger`, `RouterRuntimeHealth`.
- Produces: two new keyword-only parameters on `create_router_app`:
  `fallback_config: FallbackConfig | None = None` and
  `runtime_health: RouterRuntimeHealth | None = None`; return type remains
  `FastAPI`.

- [ ] **Step 1: Run GitNexus impact before edits**

Run:

```bash
node .gitnexus/run.cjs impact create_router_app --repo slancha-mesh \
  --branch build/oss-ready-core-split --direction upstream --depth 3
node .gitnexus/run.cjs impact chat_completions --repo slancha-mesh \
  --branch build/oss-ready-core-split --file mesh/router_app.py \
  --direction upstream --depth 3
node .gitnexus/run.cjs impact _reachable_bindings --repo slancha-mesh \
  --branch build/oss-ready-core-split --direction upstream --depth 3
```

Expected: HIGH risk because router creation and completion handling sit on the request path. Report direct callers and affected processes before editing.

- [ ] **Step 2: Replace old auto-route 503 assertions with the typed contract**

Assert:

```python
assert response.status_code == 503
assert response.headers["X-Slancha-Outcome"] == "punt"
assert response.json()["error"]["type"] == "slancha_punt"
assert response.json()["error"]["code"] == "no_suitable_local_route"
```

Add non-streaming tests for:

- no local route and no proxy permission → punt, no fallback request;
- no local route and `X-Slancha-Allow-Fallback: proxy` → configured fallback;
- all local bindings time out, permitted proxy succeeds;
- `deny` overrides a permissive server default;
- caller bearer never reaches fallback while configured bearer does;
- fallback model replaces `auto` or specialist ID;
- cloud 5xx returns bounded 502 and increments failure counters;
- non-retriable local 4xx returns directly and does not open a circuit.

- [ ] **Step 3: Run focused tests and confirm old behavior fails**

Run:

```bash
uv run python -m pytest -q \
  mesh/tests/test_router_auto.py \
  mesh/tests/test_router_app.py
```

Expected: new typed-punt and fallback tests fail against the old 503/502 branches.

- [ ] **Step 4: Implement orchestration helpers**

Add private helpers with these signatures:

```python
def _punt_response(
    *, code: str, message: str, reason: str,
    local_attempts: int, retryable: bool,
) -> JSONResponse:
    return JSONResponse(status_code=503, content=punt_payload(
        code, message, local_attempts=local_attempts, retryable=retryable,
    ), headers={"X-Slancha-Outcome": "punt", "X-Slancha-Reason": reason[:512]})


async def _proxy_fallback_response(
    client: httpx.AsyncClient,
    *, config: FallbackConfig, body: dict[str, Any], reason: str,
) -> Response:
    fallback_body = {**body, "model": config.model}
    upstream = await client.post(
        config.endpoint, json=fallback_body, headers=config.headers(),
        timeout=config.timeout,
    )
    return Response(content=upstream.content, status_code=upstream.status_code)


async def _escalate_non_streaming(
    client: httpx.AsyncClient,
    *, config: FallbackConfig, trigger: FallbackTrigger,
    permission_header: str | None, body: dict[str, Any],
    reason: str, local_attempts: int,
    runtime_health: RouterRuntimeHealth,
) -> Response:
    if not config.allows(trigger, permission_header):
        runtime_health.record_punt()
        return _punt_response(
            code="no_suitable_local_route", message="No healthy local route.",
            reason=reason, local_attempts=local_attempts, retryable=True,
        )
    response = await _proxy_fallback_response(
        client, config=config, body=body, reason=reason,
    )
    runtime_health.record_cloud_fallback(success=response.status_code < 500)
    return response
```

`chat_completions` must keep the original client body for fallback and use a
separate rewritten copy for local nodes. Record each retriable local result
against `binding_key(specialist_id, binding.node_id)`. Filter open circuits
before capping fallback attempts. Return the existing local 4xx response
without escalation.

- [ ] **Step 5: Run focused tests and Ruff**

Run:

```bash
uv run python -m pytest -q \
  mesh/tests/test_fallback.py \
  mesh/tests/test_runtime_health.py \
  mesh/tests/test_router_auto.py \
  mesh/tests/test_router_app.py
uv run ruff check mesh/router_app.py mesh/tests/test_router_auto.py \
  mesh/tests/test_router_app.py
```

Expected: all commands pass.

- [ ] **Step 6: Review affected flows and commit**

Run:

```bash
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope unstaged
git add mesh/router_app.py mesh/tests/test_router_auto.py \
  mesh/tests/test_router_app.py
git commit -m "Escalate exhausted local routes explicitly"
```

Expected: HIGH risk limited to router request flows; all direct callers remain covered by tests.

### Task 5: Add streaming fallback and richer health

**Files:**
- Modify: `mesh/router_app.py`
- Modify: `mesh/tests/test_router_app.py`

**Interfaces:**
- Consumes: the Task 4 fallback helpers and `RouterRuntimeHealth`.
- Produces streaming cloud fallback plus backward-compatible `GET /health` fields.

- [ ] **Step 1: Run impact analysis**

Run:

```bash
node .gitnexus/run.cjs impact _proxy_stream_with_fallback --repo slancha-mesh \
  --branch build/oss-ready-core-split --direction upstream --depth 3
```

Expected: direct caller `chat_completions`; streaming request flow is HIGH risk.

- [ ] **Step 2: Write failing streaming and health tests**

Add tests proving:

- all local stream opens fail, permitted fallback streams real SSE bytes;
- the fallback receives its configured model and credential only;
- no retry occurs after the first fallback byte reaches the client;
- a denied fallback returns the same typed 503 punt before response start;
- two local timeouts open a circuit and reduce `specialists_routable` while
  leaving `specialists_reachable` unchanged;
- cooldown plus a successful request restores `specialists_routable`;
- health reports `open_circuits`, aggregate queue depth, request success,
  timeout, punt, and cloud-fallback counters;
- health output contains no prompt or completion content.

- [ ] **Step 3: Run the tests and confirm failure**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_router_app.py -k \
  'stream and fallback or health or circuit'
```

Expected: the new streaming and health assertions fail.

- [ ] **Step 4: Implement streaming escalation and health fields**

Introduce `LocalRoutesExhausted(Exception)` with bounded fields instead of
raising `HTTPException` inside `_proxy_stream_with_fallback`. Catch it in
`chat_completions` and call:

```python
async def _proxy_fallback_stream(
    client: httpx.AsyncClient,
    *, config: FallbackConfig, body: dict[str, Any], reason: str,
    runtime_health: RouterRuntimeHealth,
) -> StreamingResponse:
    request = client.build_request(
        "POST", config.endpoint,
        json={**body, "model": config.model}, headers=config.headers(),
    )
    response = await client.send(request, stream=True)
    return _streaming_fallback_response(
        response=response, reason=reason, runtime_health=runtime_health,
    )
```

Add `_streaming_fallback_response(*, response: httpx.Response, reason: str,
runtime_health: RouterRuntimeHealth) -> StreamingResponse`. Its async generator
yields `response.aiter_bytes()`, calls `response.aclose()` in `finally`, records
success only after normal exhaustion, and adds the cloud-fallback headers.

The generator owns and closes the fallback stream context. It passes upstream
bytes unchanged, adds `X-Slancha-Outcome: cloud_fallback`, and records success
only after the stream closes normally.

The health handler calculates discovery counts from the snapshot and routable
counts through runtime circuit state. Aggregate queue depth is the sum of
non-negative binding queue depths. Snapshot age is
`max(0, now_utc - snapshot.snapshot_ts)` in seconds.

- [ ] **Step 5: Run focused tests and Ruff**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_router_app.py
uv run ruff check mesh/router_app.py mesh/tests/test_router_app.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit**

Run:

```bash
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope unstaged
git add mesh/router_app.py mesh/tests/test_router_app.py
git commit -m "Expose request-ready mesh health"
```

### Task 6: Wire CLI configuration and startup validation

**Files:**
- Modify: `mesh/cli.py`
- Modify: `mesh/tests/test_cli.py`

**Interfaces:**
- Consumes: `FallbackConfig` and the extended `create_router_app` signature.
- Produces router flags and environment defaults.

- [ ] **Step 1: Run impact analysis**

Run:

```bash
node .gitnexus/run.cjs impact cmd_router --repo slancha-mesh \
  --branch build/oss-ready-core-split --direction upstream --depth 3
node .gitnexus/run.cjs impact build_parser --repo slancha-mesh \
  --branch build/oss-ready-core-split --file mesh/cli.py \
  --direction upstream --depth 3
```

Expected: CLI startup flow is MEDIUM or HIGH risk; direct callers are `main` and tests.

- [ ] **Step 2: Write failing parser and validation tests**

Test these exact flags:

```text
--fallback-mode punt|proxy
--fallback-url URL
--fallback-model MODEL
--fallback-api-key-env NAME
--fallback-trigger unroutable|unavailable|both
--fallback-default deny|proxy
--fallback-connect-timeout SECONDS
--fallback-read-timeout SECONDS
```

Assert proxy mode without URL/model exits before `uvicorn.run`, and that a
complete configuration reaches `create_router_app` as one `FallbackConfig`.

- [ ] **Step 3: Run focused tests and confirm failure**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_cli.py -k fallback
```

Expected: parser rejects the new arguments.

- [ ] **Step 4: Implement parser and injection**

Build the config in `cmd_router`, call `validate()`, and pass it to
`create_router_app`. Environment variables use the same names prefixed by
`SLANCHA_FALLBACK_`. CLI values win over environment defaults. Error messages
name the invalid field without echoing a credential value.

- [ ] **Step 5: Run focused tests, help smoke, and Ruff**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_cli.py
uv run slancha-mesh router --help
uv run ruff check mesh/cli.py mesh/tests/test_cli.py
```

Expected: tests and lint pass; help lists all fallback flags.

- [ ] **Step 6: Commit**

Run:

```bash
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope unstaged
git add mesh/cli.py mesh/tests/test_cli.py
git commit -m "Configure router fallback explicitly"
```

### Task 7: Prove the real socket path and close the routing slice

**Files:**
- Create: `mesh/tests/test_router_live_socket.py`
- Modify only if a live proof exposes a defect: routing files from Tasks 2–6.

**Interfaces:**
- Consumes: installed `slancha-mesh`, uvicorn, local ephemeral ports, and controlled FastAPI node/fallback apps.
- Produces: an in-situ test that drives a real HTTP client through a real router socket to a real upstream socket.

- [ ] **Step 1: Write the socket test**

Start three subprocesses on OS-assigned loopback ports:

1. a node-info/OpenAI stub whose completion endpoint times out or returns 503;
2. a fallback OpenAI stub that records headers/model and returns
   `{"choices":[{"message":{"content":"FALLBACK-OK"}}]}`;
3. `slancha-mesh router` configured with the node peer and fallback target.

Drive the router with `httpx.Client`, first without the fallback header and
then with `X-Slancha-Allow-Fallback: proxy`. Assert typed punt first,
`FALLBACK-OK` second, configured fallback model, absent caller bearer, and
eventual `specialists_routable == 0` after the failure threshold.

- [ ] **Step 2: Run the live test**

Run:

```bash
uv run python -m pytest -q mesh/tests/test_router_live_socket.py -s
```

Expected: PASS through real sockets and subprocess startup/shutdown.

- [ ] **Step 3: Run the deterministic routing gate**

Run:

```bash
uv run python -m pytest -q \
  mesh/tests/test_fallback.py \
  mesh/tests/test_runtime_health.py \
  mesh/tests/test_router_auto.py \
  mesh/tests/test_router_app.py \
  mesh/tests/test_router_live_socket.py \
  mesh/tests/test_cli.py \
  mesh/tests/test_usage_emitter.py \
  mesh/tests/test_tailnet.py
uv run ruff check mesh
uv run python -m mesh.validate_card --strict
```

Expected: every command passes.

- [ ] **Step 4: Run the full suite**

Run:

```bash
uv run python -m pytest -q
```

Expected: all non-environment-gated tests pass; skips match declared live dependency gates.

- [ ] **Step 5: Probe Paul's running router without changing services**

Run:

```bash
curl -fsS --max-time 5 http://127.0.0.1:8080/health
curl -sS --max-time 120 http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-14b-q4-ollama","messages":[{"role":"user","content":"Reply with exactly MESH-ROUTE-OK"}],"temperature":0}'
```

Expected: record the actual result. Do not claim personal-fleet success if the running old router or Spark node still times out.

- [ ] **Step 6: Run semantic review**

Build the build-loop JSON packet with goal, boundaries, acceptance criteria,
the routing diff, caller evidence, exact test output, and assumptions. Keep it
under 17,000 bytes. Run:

```bash
BARKEEP_BIN="uv run --project /Users/laul_pogan/Source/barkeep barkeep" \
python3 /Users/laul_pogan/Source/dotfiles-claude/skills/build-loop/scripts/review.py \
  --writer codex --reviewer auto < /tmp/slancha-routing-review.json
```

Expected: exit 0 with no BLOCKER or MAJOR findings. Fix accepted findings and
rerun focused, live, and full gates. Maximum three semantic cycles.

- [ ] **Step 7: Detect final scope and commit the live proof**

Run:

```bash
node .gitnexus/run.cjs detect-changes --repo slancha-mesh \
  --branch build/oss-ready-core-split --scope compare --base-ref origin/main
git add mesh/tests/test_router_live_socket.py
git commit -m "Verify router escalation through real sockets"
```

Expected: changes affect only intended routing, CLI, health, and tests. Keep
unrelated user files unstaged.
