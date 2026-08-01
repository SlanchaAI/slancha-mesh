# OSS Core and Optional Tuning Add-on

**Status:** Approved on 2026-08-01  
**Audience:** Maintainers and future contributors  
**Implementation branch:** `build/oss-ready-core-split`

## Decision

Slancha-Mesh will ship two Python distributions from one repository:

- `slancha-mesh` owns model serving, discovery, routing, health, durable
  services, and cloud escalation.
- `slancha-mesh-tune` owns fine-tuning, corpus construction, replay-driven
  evaluation, promotion gates, and training coordination.

The core distribution will never import the tuning distribution. The tuning
distribution will depend on a compatible core version and use public core
models, files, and HTTP endpoints. This one-way dependency keeps serving usable
when the tuning add-on is absent.

Slancha-Mesh will choose local routes. It will not choose cloud vendors or
manage a portfolio of paid providers. When local execution cannot satisfy a
request, the router will return a typed punt or proxy to one operator-configured
OpenAI-compatible upstream. Barkeep remains the outer quota, capability, and
spend-policy layer.

## Evidence Behind the Decision

The 2026-08-01 audit found:

- The full suite passed: 1,052 tests passed and 19 skipped.
- Ruff passed after installing the declared development extra.
- A clean wheel install ran `slancha-mesh --help` and `plan --json`.
- The wheel contained 187 files. It shipped tests, tuning modules, the legacy
  evaluation dashboard, deploy scripts, and a tool cache. The wheel was 20 MB;
  the source distribution was 193 MB.
- `ServeDaemon` imported `IdleDetector`, `TrafficReplayStore`, and
  `TrainingPass`. Core serving tests emitted warnings from a placeholder
  training path.
- `slancha-mesh loop run` advertised a runtime whose real train-and-evaluate
  executor was deliberately unwired.
- `select_mesh_route` appended a cloud terminus, but `router_app` returned 503
  when the classifier selected it. Cloud fallback was a label, not execution.
- The live router reported one reachable specialist, yet two inference probes
  failed with a 502 or timeout. Discovery health did not represent request
  readiness.
- The live classifier labeled a trivial exact-response prompt as economics,
  tools, jailbreak, and personally identifiable information. The router needs
  explicit-model operation and visible classifier evidence while the
  classifier is calibrated.
- The active LAN branch contained two commits. `origin/main` contained five
  later usage-telemetry commits that the implementation must preserve.
- The public repository existed, but PyPI had no `slancha-mesh` project. The
  README described an unavailable registry install and lacked standard trust
  surfaces.

## Goals

1. Serve local models through Ollama, vLLM, llama.cpp, MLX, or adopted
   OpenAI-compatible endpoints.
2. Discover nodes on an explicit LAN peer list or a Tailscale/Headscale
   tailnet.
3. Route explicit model requests and optional `model: "auto"` requests across
   healthy nodes.
4. Escalate requests through a typed punt or one explicit generic upstream.
5. Prevent request failures from leaving a broken node advertised as ready.
6. Install node and router processes as durable systemd, launchd, or Windows
   services.
7. Ship fine-tuning as an optional, independently testable add-on.
8. Produce small, truthful release artifacts with a reproducible first-value
   path.

## Non-goals

- Implement a multi-provider cloud router inside Slancha-Mesh.
- Copy Barkeep's quota, subscription, price, affinity, or spend policy.
- Turn the fleet dashboard into a control plane.
- Download model weights during installation or tests.
- Publish to PyPI, merge branches, or change public infrastructure in this
  build.
- Promise that classifier output measures quality. The classifier supplies
  routing signals; observed request outcomes supply health evidence.

## Package Boundary

### Core distribution: `slancha-mesh`

Core keeps these responsibilities:

- CLI: `plan`, `up`, `doctor`, `discover`, `status`, `serve`, `router`, `node`,
  and `service`.
- Backends and adopted endpoints.
- Node probes, catalog cards, node identity, authentication, and URL guards.
- Discovery, registry snapshots, route selection, request proxying, and usage
  telemetry.
- The optional classifier and its routing interface.
- Runtime outcome tracking and health reporting.
- Durable node and router service rendering and installation.

Core removes these responsibilities:

- Fine-tuning and checkpoint promotion.
- Training-idle state machines and GPU training reservations.
- Corpus construction, curation, replay grading, holdouts, and judge runners.
- The autonomous experiment loop and legacy evaluation dashboard.
- Training smoke scripts and training corpora.

The base CLI will stop advertising `loop`. Core will not expose commands that
cannot execute a real production path.

### Add-on distribution: `slancha-mesh-tune`

The add-on will expose `slancha-mesh-tune` and contain:

- Training passes and checkpoint metadata.
- Replay storage and corpus builders.
- Curation, grading, holdout evaluation, promotion gates, and cloud spot
  checks.
- The experiment queue and loop runner, wired to a real execute function.
- Training-specific GPU reservation and idle coordination.
- The existing evaluation dashboard.

The add-on will import core contracts through documented modules. Core will
not probe for the add-on or silently enable training. Operators will start the
add-on explicitly.

### Repository and build layout

The repository will use a uv workspace with two build roots:

```text
packages/
  slancha-mesh/          # core pyproject and package
  slancha-mesh-tune/     # add-on pyproject and package
```

The core package will move to `packages/slancha-mesh/mesh`; the add-on package
will use `packages/slancha-mesh-tune/mesh_tune`. The move will land as one
mechanical commit before behavior changes, preserving import names inside the
core distribution. Each published artifact will use an explicit include list.
The core source distribution will exclude corpora, tests, screenshots, session
logs, tool caches, and add-on sources. Test sources remain in the repository
and run against both installed distributions.

## Request Flow and Escalation

### Local route

1. The router authenticates and bounds the request.
2. An explicit model selects that specialist directly. `model: "auto"` invokes
   the optional classifier.
3. Selection filters discovery candidates through runtime circuit state.
4. The router attempts at most the configured local fallback count.
5. A successful response records the winning node, closes its circuit, emits
   optional neutral usage telemetry, and returns routing headers.

### Typed punt

When classification finds no suitable specialist, request policy forbids
fallback, or every local attempt fails, the router produces one normalized
escalation outcome:

```json
{
  "error": {
    "type": "slancha_punt",
    "code": "no_suitable_local_route",
    "message": "No healthy local specialist satisfies this request.",
    "details": {
      "local_attempts": 0,
      "suggested_class": "cloud",
      "retryable": true
    }
  }
}
```

The response includes `X-Slancha-Outcome: punt` and `X-Slancha-Reason`. The
router returns 503 for this contract, preserving the current auto-route status
while adding a stable error body. Existing OpenAI-compatible error fields
remain present so generic clients can display the failure.

### Optional generic upstream

An operator may configure one fallback target with:

- mode: `punt` or `proxy`;
- base URL;
- model alias;
- credential environment variable name;
- connect, read, and total timeouts;
- fallback trigger: unroutable, unavailable, or both.

`punt` is the default. `proxy` requires complete startup configuration and an
explicit activation flag. The router fails startup on partial configuration.
It never forwards the caller's authorization header to the fallback target.
It sends only the configured fallback credential.

Fallback permission has two gates. The server must configure and enable the
target, and the request must include `X-Slancha-Allow-Fallback: proxy`. A
standalone operator may set `--fallback-default proxy` at router startup to
authorize requests that omit the header. The default remains `punt`. Paul's
Barkeep-facing router will retain that default, so a request selected as a free
mesh lane cannot spend through the fallback target. A caller may send
`X-Slancha-Allow-Fallback: deny` to override a permissive server default.

The router supports streaming and non-streaming fallback. It records
`X-Slancha-Outcome: cloud_fallback`, the local failure reason, and the configured
fallback alias. A failed cloud request returns a bounded 502 error containing
local and fallback failure classes without secret values or response bodies.

The server and request gates together authorize the configured network call.
They do not authorize provider selection, account creation, key retrieval, or
a change to Barkeep's spend policy.

## Runtime Health

Discovery proves that node metadata is readable. It does not prove that an
inference request will finish. The router will therefore maintain bounded,
in-memory outcome state per binding:

- consecutive retriable failures;
- circuit state: closed, open, or half-open;
- cooldown deadline;
- last success time;
- last failure class;
- recent latency from completed requests.

A timeout, transport error, or retriable 5xx increments the failure count.
Crossing the threshold opens the circuit for a bounded cooldown. Selection
skips open circuits. A half-open probe request closes the circuit on success or
reopens it on failure. Non-retriable client errors do not penalize a node.

`GET /health` remains backward compatible and adds:

- discovered nodes and specialists;
- routable specialists after circuit filtering;
- open-circuit count;
- aggregate queue depth;
- snapshot age;
- recent request success, timeout, and fallback counts;
- degraded reasons.

`specialists_reachable` remains during migration. Barkeep should move to the
new routable field after its mesh tap gains compatibility tests. The fleet
dashboard may consume the same health fields or the richer node registry. Mesh
remains the producer; both projects remain consumers.

## Durable Services

The service installer will support two real roles:

```text
slancha-mesh service install node -- <up arguments>
slancha-mesh service install router -- <router arguments>
```

Node services run `slancha-mesh up`. Router services run
`slancha-mesh router`. Rendered units use absolute executable paths, explicit
working/state directories, restart policies, and log destinations. Dry-run
prints the exact artifact and management commands.

Backward-compatible parsing will accept the existing node form for one release.
The implementation will verify a systemd node unit and a launchd router plist
in their real service managers before declaring the durability slice complete.

## Barkeep and Fleet Dashboard Seams

Barkeep owns task class, capability floors, quota drawdown, subscription
affinity, provider choice, and paid-spend vetoes. Slancha-Mesh supplies one
local execution pool and truthful health. This separation prevents a local
route from duplicating Barkeep's policy or spending through an account that
Barkeep vetoed.

The first cross-repository compatibility change will be read-only in effect:
Barkeep's mesh tap will prefer `specialists_routable` and use aggregate queue
pressure when present, while retaining support for `specialists_reachable`.
A separate executor design is required before Barkeep can retry the same task
after a typed punt; its current charter advises and records but does not spawn.

The fleet dashboard remains observational. It may read router health and node
registry data for queue depth, latency, model throughput, and circuit state.
Slancha-Mesh will not read dashboard state to make routing decisions.

## Compatibility and Migration

- Preserve the existing OpenAI-compatible endpoints and explicit specialist
  IDs.
- Preserve `specialists_reachable` until Barkeep and external clients migrate.
- Preserve LAN and tailnet discovery.
- Preserve the neutral usage telemetry merged on `origin/main`.
- Move Python symbols with compatibility imports only when a known external
  caller exists. Remove compatibility imports after a documented deprecation
  window.
- Reject an installed core/add-on version mismatch with a clear startup error.
- Do not auto-install the add-on from core.

The implementation branch will merge `origin/main` and retain the two LAN
commits. It will not rewrite the user's existing files or push without a new
request.

## Error and Safety Rules

- Bound request bodies, response reads, fallback attempts, health history, and
  error text.
- Resolve and validate fallback URLs at startup and before redirects. Disable
  redirects unless the redirected target passes the same URL policy.
- Keep node and fallback credentials separate.
- Strip caller authorization before every node or fallback request.
- Treat missing token counts as unavailable, never zero.
- Log failure classes and targets without credentials, prompts, or completion
  bodies.
- Require explicit configuration before any cloud request.
- Keep model downloads and heavy engine installation behind human approval.

## Implementation Slices

### Slice 1: integrate and make routing truthful

- Merge `origin/main` into the implementation branch.
- Preserve LAN advertise-host behavior and the GB10 Qwen card.
- Add typed punt, explicit generic fallback, runtime circuits, and richer
  health.
- Add focused and full tests.
- Prove explicit local routing and fallback or punt through the real router.

### Slice 2: split the tuning add-on and artifacts

- Create the workspace and add-on distribution.
- Move tuning commands and modules behind the one-way package boundary.
- Remove the unwired loop command from core.
- Build and inspect both wheels and source distributions.
- Run clean-environment core-only and core-plus-add-on tests.

### Slice 3: durable personal fleet and OSS surfaces

- Add real node and router service roles.
- Install and verify the Spark node under systemd and the Mac router under
  launchd, subject to host access and existing-service safety checks.
- Update Barkeep's mesh tap compatibility if that repository is in scope for a
  separate committed change.
- Verify fleet dashboard consumption without making it a routing dependency.
- Repair README claims and add the minimum security, contribution, changelog,
  and release documentation required by the final OSS audit.

Each slice uses GitNexus impact analysis before symbol edits,
`detect_changes --base-ref origin/main` before commits, deterministic tests,
an in-situ caller, and a fresh semantic review.

## Acceptance Criteria

The work is complete when all of these statements have direct evidence:

1. A clean core-only environment installs and runs serving, discovery, router,
   and service commands without tuning dependencies or imports.
2. A clean add-on environment installs `slancha-mesh-tune` and executes a real
   queued evaluation or training path; no advertised command is unwired.
3. Core artifacts exclude tests, corpora, tool caches, session logs, legacy
   dashboards, and tuning sources.
4. An explicit local request succeeds through the real router.
5. An unsuitable or failed local request produces the documented typed punt.
6. With explicit proxy configuration, the same request reaches a controlled
   OpenAI-compatible fallback without forwarding caller credentials.
7. A repeatedly failing node opens its circuit and stops counting as routable;
   a successful half-open request restores it.
8. Barkeep can distinguish discovered capacity from routable capacity and
   preserves its spend veto.
9. The fleet dashboard can display the new health data without becoming a
   mesh dependency.
10. The node and router survive process termination under their real service
    managers.
11. Focused tests, the full suite, Ruff, strict catalog validation, artifact
    inspection, and clean-room installs pass.
12. The README's install and first-value path matches a public distribution or
    an explicit source install, and the final OSS Ready audit records no P0
    contradiction.

## Rollback

Each slice lands in atomic commits. The old `specialists_reachable` field and
explicit specialist routing remain throughout the migration. Operators can
disable cloud proxying by selecting `punt`, stop using the add-on without
changing core, and uninstall either service role through the existing service
command. No slice requires a destructive data migration.
