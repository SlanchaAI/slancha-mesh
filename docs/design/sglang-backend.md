# SGLang backend plan

Plan for SlanchaAI/slancha-mesh#152. Documentation only: no backend is implemented
or enabled by this change. Source anchors below refer to commit
`2bf33053294ab181ba0d4fd3d11c234332cee28c`; locate the named symbols if lines move.
No engine installation, model download, serving request, or benchmark is part of
this row. vLLM remains primary; SGLang requires an explicitly selected card.

## Exact implementation edits

| Source anchor | Planned edit |
| --- | --- |
| `mesh/models.py:27` (`Backend`) | Append `"sglang"` to the literal. Existing consumers `NodeProbe.available_backends` at line 87 and `SpecialistCard.required_backend` at line 127 then accept it without new fields. Preserve argument guards at lines 183–199 and provenance fields at lines 179–181. |
| `mesh/probe.py:289` (`_detect_backends`) | Before the return at line 306, append `"sglang"` iff `_try_import("sglang")` is true. Reuse the exception handling in `_try_import` at line 309. |
| `mesh/backends.py:111` (`VLLMBackend`) | Add an adjacent, independent `@dataclass SGLangBackend` implementing the protocol at line 49. Mirror lifecycle structure, not vLLM flags or metrics. Update module description at line 7 and exports near line 1122. No base-class refactor. |
| `mesh/serve.py:32` (backend imports) | Import `SGLangBackend`. |
| `mesh/serve.py:291` (`build_backend`) | After the vLLM branch, dispatch `required_backend == "sglang"` to `SGLangBackend(card=card, host=bind_host, port=port, log_path=log_path)`. Keep existing log-directory setup at line 286. Do not silently return `NullBackend` on launch failure. |

Detection deliberately matches the launch interpreter: use `sys.executable` for
launch and probe the top-level Python package in the current environment. A
PATH-only executable from another environment is insufficient. Do not import
SGLang, probe a dotted submodule (which can import its parent), run `--help`, or
contact a server during detection. Presence is an installation hint, not evidence
of supported hardware, a complete server installation, or readiness. Missing
server dependencies must fail visibly at launch. No CUDA capability gate or
automatic engine preference change belongs in this slice.

## Launch and lifecycle contract

Proposed initial fields: `card`, `host="127.0.0.1"`, `port=8001`, `log_path=None`,
`name="sglang"`, and private `_proc=None`. `base_url` is the origin
`http://{host}:{port}`, without `/v1`; OpenAI-compatible consumers append their
API paths. Use `card.specialist_id` as served model name, matching vLLM and the
mesh's card identity.

Proposed argv, passed as a list with no shell:

```python
[
    sys.executable, "-m", "sglang.launch_server",
    "--model-path", card.model_id,
    "--served-model-name", card.specialist_id,
    "--host", host, "--port", str(port),
]
```

Append `--revision`, then `card.revision`, only when set. Append
`--trust-remote-code` only when explicitly enabled on the card; never enable it
by default. Retain the existing leading-dash validators. Do not translate
vLLM's memory, context, eager-execution, or Blackwell workaround flags by guess;
use SGLang defaults for the initial adapter. Inherit the environment without
injecting vLLM-specific settings.

This is a proposed adapter contract, not a verified SGLang version claim. This
network-free design pass did not inspect an upstream release. Before coding,
verify the launch module, these flags, revision handling, model alias and
`/health` behavior against the chosen release's primary source; record the
release and source in the implementation PR. Do not drop provenance or remote
code safeguards if a flag differs. No version or new dependency is adopted here.

Mirror `VLLMBackend.start` at `mesh/backends.py:144`: nonblocking spawn,
idempotent while the owned child is alive, `start_new_session=True`, log opened
in append-binary mode, stderr redirected to stdout, and DEVNULL when no log is
configured. Close the parent's log descriptor in `finally`, including spawn
failure. Propagate launch errors to `ServeDaemon.start` (`mesh/serve.py:122`),
which already logs failures and marks startup unsuccessful.

Explicit ownership difference: reject an occupied port with a clear error;
do not adopt an arbitrary listener as SGLang. Reuse the shape of the local port
check at `mesh/backends.py:296`, but never `_find_pid_on_port` adoption. Operators
with an existing server can use the existing external backend. `is_alive`
checks only the owned child's `poll()`; never-started or exited means false.

Mirror readiness polling at `mesh/backends.py:222`: check child liveness, poll
`/health` with a bounded request timeout, accept 200, retry HTTP/OS errors and
non-200 responses until the deadline, and return false on exit or timeout.
Mirror owned-process shutdown at line 249: terminate the process group, wait
up to the supplied timeout, escalate to SIGKILL if necessary, and clear ownership
state. Handle an already vanished process and repeated stop safely. Never signal
an unowned process.

For the first adapter, `utilization()` returns `{"queue_depth": 0, "running": 0}`
as unavailable measurements, following the existing backend fallback convention.
Do not reuse `_parse_vllm_metrics` (`mesh/backends.py:321`) or claim measured zero
load. Engine-specific telemetry and performance comparison remain follow-ups.

## Wiring and offline proof for implementation

The concrete caller chain is `build_daemon` (`mesh/serve.py:393`, factory call at
line 446) → `build_backend` → `SGLangBackend` → `ServeDaemon.start`, heartbeat,
and stop. An explicit test card with `required_backend="sglang"` exercises this
branch; adding an unused class alone is insufficient. Existing catalog cards,
engine selection policy and deployment configuration stay unchanged.

Add `mesh/tests/test_backends_sglang.py`, following `_FakePopen` and argv capture
patterns in `mesh/tests/test_backends.py`. Add focused cases to
`mesh/tests/test_probe.py` and `mesh/tests/test_serve.py`:

- Model/probe: valid SGLang card and probe round-trip; unknown backend remains
  rejected. Mock `_try_import` and `shutil.which` for present/absent SGLang and
  PATH-only installation; preserve existing backend detection. Exercise
  `find_spec` absence and ImportError/ValueError handling without importing an
  engine or invoking hardware probes.
- Launch: mock `mesh.backends.subprocess.Popen`, the port check, and environment.
  Assert complete argv, current interpreter, alias, host/port, optional revision,
  trust default-off/explicit-on, session flag, and log redirection. Assert no
  vLLM flags injected. Verify log handles close on success and spawn exception.
- Ownership: repeated start spawns once; dead child can restart; busy port raises
  without spawning or adopting. Never-started/dead-child liveness is false.
- Readiness: mock `mesh.backends.httpx.get`, time and sleep. Cover immediate 200,
  retry then success, HTTP/OS errors, non-200 timeout and early child exit.
- Shutdown: fake child and mock `os.getpgid`, `os.killpg`, `os.kill`, time and
  sleep. Cover normal termination, timeout escalation, vanished process and
  repeated stop; assert no real signal or unowned-process action occurs.
- Metrics: fallback dictionary has the expected shape and performs no HTTP call.
- Factory/caller: construct through `build_backend`, assert SGLang type and
  propagated card, bind host, port and log path; retain a vLLM regression case.
  Supply explicit catalog and probe to `build_daemon` so it never probes hardware;
  mock node-key resolution. Start with fake subprocess and health response,
  inspect heartbeat locally for specialist/model identity and advertised URL,
  then stop. Assert spawn was observed through the actual factory branch. Also
  cover failed readiness and absence of a healthy loaded model. Never post a
  heartbeat or contact a registry.

All subprocesses, sockets, HTTP calls, signal operations and clocks used by these
cases must be mocked; use temporary log paths. No SGLang package, GPU, downloaded
weights, real listener or network is required. Suggested focused implementation
check: `uv run python -m pytest -q mesh/tests/test_backends_sglang.py
mesh/tests/test_probe.py mesh/tests/test_serve.py mesh/tests/test_backends.py`.
These tests are planned, not added or claimed passing by this documentation PR.

## Proof for this row

```sh
test -s docs/design/sglang-backend.md && grep -qiE 'sglang' docs/design/sglang-backend.md
```

Only `docs/design/sglang-backend.md` changes. Runtime support, live serving and
bake-off results remain unimplemented; this row delivers the implementation plan.
