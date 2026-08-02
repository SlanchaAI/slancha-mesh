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
- vLLM Semantic Router is the supported caller-facing router and remains an
  optional install for Docker-free hosts. Mesh stays behind it as the dynamic
  private-fleet router.
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
- The first remote PR run exposed a wall-clock race in the live-socket circuit
  test and invalid assumptions that `slancha-shared` and `slancha-local` were
  public. The circuit test now advances an injected clock. Private integration
  jobs now run only when maintainers configure `SLANCHA_CROSS_REPO_TOKEN`, so
  public forks retain a green in-repo CI path without access to private repos.
- Secret-pattern scan found no committed private key, Tailscale key, or common
  API-key pattern.
- GitNexus compare-to-`origin/main` reports CRITICAL breadth: 49 files, 251
  symbols, 23 flows. This is expected for the package split and request-path
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

## vLLM Semantic Router front-door cutover

- Promoted pinned vLLM Semantic Router v0.3.0 from a composition example to
  the supported caller-facing front door on host port 8888. Slancha Mesh stays
  private on port 8080 and owns live fleet discovery, request readiness,
  circuits, node transport, and typed punts.
- Isolated the upstream runtime with stack name `slancha-mesh`, port offset
  100, pinned router/Envoy/simulator images, and a dedicated Python 3.11 virtual
  environment under `~/.local/state/slancha-mesh/vllm-semantic-router`.
- Forced local Docker Desktop on macOS. The interactive shell's active Docker
  context was `dellpromax`; without the override, the upstream CLI split pulls
  and container creation across local and remote Docker engines.
- Bound Envoy to the container interface. A loopback bind inside the container
  made Docker's published host port reset every request.
- Added an isolated Python shim around the pinned upstream CLI that rewrites
  every Docker/Podman `run` or `create` published port to host loopback. It
  intercepts both `subprocess.run` and `Popen`, including `sudo docker` and
  `docker container create` forms. This covers Envoy, router control ports,
  Redis, Postgres, and the simulator rather than hardening only the caller port.
- Added a post-start fail-closed inspection of every container attached to the
  dedicated `slancha-mesh-vllm-sr-network`. Any non-loopback host binding stops
  that network's containers; internal-only containers with no published ports
  remain valid.
- Added a state-aware `semantic-router supervise` loop and made the service
  installer use it. Upstream `serve` exits after provisioning containers and a
  naive launchd `KeepAlive` repeatedly tears down healthy containers.
- Added `/usr/local/bin` and `/opt/homebrew/bin` to the launchd runtime PATH.
  The first service attempt otherwise restarted continuously because launchd
  could not resolve Docker.
- Configured vLLM's supported static selector for the single dynamic Mesh
  backend and disabled model selection, semantic cache, and tools indexing.
  Learned selectors only choose among multiple `modelRefs`; duplicating the
  changing Mesh catalog in static vLLM YAML would create stale readiness.
- vLLM v0.3 minimal mode still starts Redis, Postgres, and the fleet simulator,
  and still initializes the default embedding runtime. This is upstream
  behavior, not a Slancha dependency or hidden fallback. A previously partial
  Hugging Face snapshot was completed so startup no longer fails model-file
  loading, though the disabled selector reports `embedding_ready=false`.

### Live cutover proof

- `POST :8888/v1/chat/completions` returned `VLLM-MESH-LIVE-OK` from
  `qwen3:14b` on Spark. Response headers named Envoy, vLLM decision
  `local-mesh`, vLLM model `slancha-auto`, Slancha specialist
  `qwen3-14b-q4-ollama`, and node `spark-472e`.
- Stopping vLLM left direct Mesh `:8080` available and returned
  `DIRECT-MESH-ROLLBACK-OK`.
- Killing Envoy under the foreground supervisor recreated the router pair and
  returned `SUPERVISOR-RECOVERY-OK`.
- The installed launchd job `ai.slancha.mesh.semantic-router` then survived a
  killed Envoy container with one stable supervisor PID/run, recreated Envoy,
  and returned `VLLM-MESH-LOOPBACK-OK`. One cold-ish recovery took about two
  minutes; the hardened warm recovery completed in under 30 seconds.
- Live `docker ps` showed every upstream host publish on `127.0.0.1`: Envoy
  `8888`, router `8180`/`9290`/`50151`, simulator `8910`, Postgres `5532`, and
  Redis `6479`.
- Current caller path: client → vLLM Semantic Router `:8888` → Slancha Mesh
  `:8080` → Spark Ollama `qwen3:14b`. Rollback path: client → Mesh `:8080`.

### Front-door security review

- Fresh-eyes review found a MAJOR LAN-exposure regression: upstream Docker
  published the container listener declared as `0.0.0.0:8788` on every host
  interface.
- A Docker network default-bind option and a narrow upstream port-helper patch
  both failed live because Docker Desktop ignored the former and upstream
  constructs some `-p` arguments outside that helper. After the second failure,
  primary-source tracing identified `subprocess.run` as the common command
  boundary. The isolated runtime shim now enforces loopback there.
- Focused tests execute the shim against both `-p` and `--publish` forms. The
  later review cycles expanded this to bare, host-qualified, inline, `Popen`,
  `sudo docker`, and `docker container create` forms, plus null/internal-only
  Docker port maps.
- The final backstop enumerates the dedicated Docker network instead of relying
  on a container-name substring. Live membership contained exactly Envoy,
  router, simulator, Postgres, and Redis; the all-port inspection and launchd
  recovery prove the same path in situ.
- The final exact-code recovery returned `VLLM-FINAL-LIVE-OK` from Spark
  `qwen3:14b`; vLLM selected `local-mesh` / `slancha-auto`, and all five Docker
  network members remained loopback-only.

### Final gates and retrospective

- Final combined suite: `1137 passed, 16 skipped`; six expected warnings cover
  the clearly labeled tuning stub. Ruff lint passed across core and add-on;
  changed vLLM files pass the formatter check. Both wheels built, with the vLLM
  lifecycle in core and training/tuning modules only in the add-on.
- vLLM's own v0.3 validator and the final live launchd recovery passed after
  the last security changes.
- Skill retrospective disposition: `none`. The failed Docker network option
  and narrow helper patch exposed a project-specific mechanical invariant, now
  enforced by executable wrapper tests plus live Docker-network inspection.
  Existing research discipline correctly forced a primary-source spike after
  two failures, so no reusable skill or global policy change is proposed.

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
- `mesh/vllm_semantic_router.py` — pinned upstream lifecycle and durable
  supervisor for the caller-facing front door.
- `docs/superpowers/specs/2026-08-01-vllm-semantic-router-front-door-design.md`
  — front-door ownership and rollback contract.
- `docs/superpowers/plans/2026-08-01-vllm-semantic-router-front-door.md` —
  cutover and verification plan.
- `docs/research/oss-routing-2026-08-01/` — primary-source candidate atlas and
  dossiers.
- `docs/superpowers/specs/2026-08-01-oss-core-and-tuning-split-design.md` —
  architecture contract.
- `docs/superpowers/plans/2026-08-01-oss-routing-integration.md` — implementation
  and acceptance plan.
- `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md` — public project trust surface.
