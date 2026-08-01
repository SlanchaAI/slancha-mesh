# Session log — 2026-08-01 — OSS-ready local routing core

## Objective

Prepare Slancha-Mesh for personal daily use and eventual open-source
publication. Keep serving, discovery, local routing, and truthful escalation in
core. Move fine-tuning and promotion machinery into an optional add-on. Reuse
current open-source routing projects at stable protocol boundaries instead of
rebuilding their provider and semantic-routing layers.

Work ran in branch `build/oss-ready-core-split-impl` at worktree
`/Users/laul_pogan/Source/slancha-mesh-oss-ready`. The dirty primary checkout
was not modified. Nothing was merged, published to PyPI, or sent to a paid
provider.

## Decisions

- Slancha-Mesh owns private-fleet pull discovery, local backend lifecycle,
  request-ready route selection, and local failover.
- Core returns a stable typed HTTP punt when no suitable local route exists or
  every local attempt fails. It does not choose providers, read cloud keys, or
  execute paid calls. Barkeep or another caller owns spend and egress policy.
- vLLM Semantic Router is an optional semantic selector in front of the mesh.
  Inference Gateway is the preferred lean external provider executor after a
  caller-authorized punt. llama-swap is an optional per-node process manager.
- Fine-tuning, replay evaluation, promotion gates, corpus scripts, and the
  dashboard belong in `slancha-mesh-tune`, not the serving distribution.
- Discovery reachability and request readiness are distinct. Passive inference
  outcomes drive bounded per-binding circuits; `/health` exposes both views.

Full OSS research and trust priors:
`docs/research/oss-routing-2026-08-01/ATLAS.md`.

## Implementation

### Truthful local escalation

- Added `mesh/escalation.py` with `no_suitable_local_route` and
  `local_route_unavailable` outcomes.
- Punts return HTTP 503, `X-Slancha-Outcome: punt`, `X-Slancha-Reason`, and an
  OpenAI-shaped `slancha_punt` body with attempt count and retryability.
- Unknown explicit model ids remain 404 client errors. No cloud dependency,
  credential reader, or provider request entered core.

### Request-ready routing

- Added `mesh/runtime_health.py`: bounded thread-safe circuits, one half-open
  probe after cooldown, passive failure/success evidence, and request counters.
- Router paths record connect errors, retryable upstream 5xx responses,
  successful completions, client errors, and punts.
- `/health` retains `specialists_reachable` and adds
  `specialists_routable`, `bindings_routable`, `queue_depth`, snapshot age,
  degraded reasons, circuit counters, and per-binding runtime state.
- Added a real-TCP uvicorn test for local success, circuit open, zero-attempt
  suppression, and half-open recovery.

### Optional tuning distribution

- Created `packages/slancha-mesh-tune` with `slancha-mesh-tune` and
  `slancha-mesh-gate` commands.
- Moved training, replay, curation, grading, evaluation, dashboard, deployment,
  corpus, and smoke scripts out of the core wheel while retaining compatible
  `mesh.*` import names through an extended namespace path.
- Removed the dormant training thread and unwired `loop` command from the core
  serving daemon and CLI.
- Core and add-on now build and install independently. CI proves core cannot
  import `mesh.training`, then installs the add-on and proves the imports and
  command appear.

### Durable operation

- Added node and router roles to the cross-platform service installer.
- Corrected pass-through arguments so persistent commands retain their required
  `up` or `router` subcommand.
- Added repeatable non-secret `--env NAME=VALUE` persistence for systemd and
  launchd. Windows Scheduled Tasks reject this option rather than silently
  dropping it.
- On Spark, replaced the nohup node with enabled user-systemd unit
  `ai.slancha.mesh.node.service`, including
  `SLANCHA_AUTH_REQUIRED=false`. Killing PID 3944133 caused systemd to restart
  PID 3945801; `/health` recovered. User linger was already enabled.
- The Mac router was already durable under launchd label
  `ai.slancha.mesh-router`; the earlier handoff's nohup claim was stale.

### OSS and Barkeep composition

- Added validated vLLM Semantic Router v0.3 configuration and explicit
  Inference Gateway/llama-swap ownership docs under `examples/oss-routing`.
- The released Inference Gateway binary listed and called a local
  OpenAI-compatible stub through its Ollama provider, returning `GATEWAY-OK`
  with no provider credential or external request.
- Updated Barkeep branch `feat/routing-tuneup` in commit `0b17171` to prefer
  `specialists_routable`, fall back to the old health field, and rank mesh
  pressure as `queue_depth / (queue_depth + bindings_routable)`.
- Added public trust surfaces: `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`,
  corrected source-install commands, package URLs, and CI coverage for both
  distributions and the split cross-repo promotion-gate path.

## Observed execution

- Mac router `GET /v1/models` listed `auto` and
  `qwen3-14b-q4-ollama` after the Spark systemd cutover.
- A real request traversed Mac router → Spark node → Ollama `qwen3:14b` and
  returned exactly `MESH-DURABLE-OK`.
- The real-socket test returned a local completion, opened its binding circuit
  after two controlled 503s, skipped the open binding without an upstream call,
  and recovered after cooldown.
- vLLM Semantic Router v0.3 accepted
  `examples/oss-routing/vllm-semantic-router.yaml` with one listener, decision,
  and model.
- Barkeep's live tap saw one idle mesh lane against the deployed older Mac
  router through its compatibility fallback.

## Verification

- Combined suite: `1125 passed, 16 skipped`; six expected warnings exercise
  the optional tuning stub and label it as non-training.
- Ruff: core and add-on clean.
- Strict catalog: 17 cards clean.
- Barkeep: `156 passed`; focused mesh tap and Ruff checks clean.
- Clean artifact boundary: core wheel contains no tests, training, or eval
  modules; add-on wheel contains training and promotion modules; core source
  distribution contains the public policy files and no add-on/tests. Both
  add-on artifacts include the Apache `LICENSE` and `NOTICE`.
- Clean core-only virtual environment imported `mesh.serve`, could not find
  `mesh.training`, and exposed no `loop` command. Installing the add-on into
  that environment made `mesh.training`, `mesh.eval.gate`, and
  `slancha-mesh-tune check` available.
- GitHub workflow YAML parsed; cross-repo slancha-local gate parity passed
  `11 passed` against the new add-on path.
- Secret-pattern scan found no committed private key, Tailscale key, or common
  API-key pattern.
- GitNexus compare-to-`origin/main` reports CRITICAL breadth: 47 files, 222
  symbols, 32 flows. This is expected for the package split and request-path
  change; focused blast-radius checks rated the service environment path LOW
  and the route path HIGH. Full and live gates above cover the affected flows.

## Fresh-eyes review closure

An independent semantic reviewer found five majors after the first green gate.
All were reproduced and fixed before handoff:

- automatic routing now filters open circuits before choosing a specialist;
- the router preserves the selector's chosen node ahead of snapshot order;
- router services default to `ai.slancha.mesh.router` instead of overwriting the
  node unit;
- failed systemd, launchd, or Scheduled Task registration returns nonzero and
  does not print a false success message;
- `slancha-mesh-tune dashboard` launches Streamlit through its supported entry
  path and reports a missing dashboard extra without a traceback.

Seven focused regressions cover these findings. A real dashboard process then
served Streamlit health `ok` and HTTP 200 on port 18983 before clean shutdown.

## Publication status and remaining work

This is an OSS-ready candidate, not a published release. The root README,
license, notice, security policy, contributor guide, changelog, locked CI,
package artifacts, and source install path are present. Remaining release
actions require an explicit integration decision:

- review and merge the feature branch;
- choose version/tag and create the GitHub release;
- configure trusted PyPI publishing for both `slancha-mesh` and
  `slancha-mesh-tune`, then test on TestPyPI before production;
- perform the intentionally deferred deeper security pass before exposing any
  router outside a trusted LAN/tailnet;
- bring up dellpromax and decide whether to add a second request-ready node.

## Artifact catalog

- `mesh/escalation.py` — typed local-to-policy punt contract.
- `mesh/runtime_health.py` — passive request readiness and bounded circuits.
- `mesh/tests/test_router_live_socket.py` — real-TCP routing/recovery proof.
- `packages/slancha-mesh-tune/` — independently packaged tuning add-on.
- `examples/oss-routing/` — validated OSS composition examples.
- `docs/research/oss-routing-2026-08-01/` — primary-source candidate atlas and
  dossiers.
- `docs/superpowers/specs/2026-08-01-oss-core-and-tuning-split-design.md` —
  architecture contract.
- `docs/superpowers/plans/2026-08-01-oss-routing-integration.md` — implementation
  and acceptance plan.
- `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md` — public project trust surface.
