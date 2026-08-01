# vLLM Semantic Router Front Door Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the caller-facing hand-built router with a pinned, durable vLLM Semantic Router front door while retaining Slancha Mesh as the private fleet backend.

**Architecture:** vLLM Semantic Router v0.3.0 listens on loopback port 8888 and proxies its `slancha-auto` provider model to the existing Slancha Mesh router on loopback port 8080. Repository tooling owns reproducible installation, validation, startup, status, and rollback; vLLM owns semantic policy and front-door behavior.

**Tech Stack:** Python 3.10+, vLLM Semantic Router 0.3.0, Docker Desktop, launchd, canonical v0.3 YAML, pytest.

## Global Constraints

- Pin vLLM Semantic Router exactly to `0.3.0`; never use `latest` or a development channel.
- Bind both routing layers to loopback only.
- Keep the Mesh router independently runnable on port 8080.
- Write runtime state only under `~/.local/state/slancha-mesh/vllm-semantic-router`.
- Read no cloud credentials and issue no paid requests.
- Preserve a one-command rollback to direct Mesh routing.

---

### Task 1: Reproducible vLLM front-door tooling

**Files:**
- Create: `mesh/vllm_semantic_router.py`
- Create: `mesh/tests/test_vllm_semantic_router.py`
- Modify: `mesh/cli.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `VLLM_SR_VERSION`, `VllmSemanticRouterPaths`,
  `build_vllm_sr_commands()`, and a `slancha-mesh semantic-router` CLI with
  `install`, `validate`, `serve`, `status`, and `stop` actions.
- Consumes: the checked-in canonical config and an explicit runtime-state root.

- [ ] Write tests asserting the exact version pin, isolated paths, argv-only
  subprocess calls, missing-Docker failure, config validation failure, and stop
  behavior that does not terminate the Mesh router.
- [ ] Run `uv run pytest -q mesh/tests/test_vllm_semantic_router.py` and confirm
  failure because the module and CLI do not exist.
- [ ] Run GitNexus upstream impact analysis for each existing CLI symbol before
  editing it; warn before any HIGH or CRITICAL change.
- [ ] Implement the smallest command module and CLI wiring that passes the
  tests. Download/install through `uv` from `vllm-sr==0.3.0`; do not execute the
  upstream curl installer.
- [ ] Run the focused tests and Ruff.
- [ ] Commit the independently working tooling.

### Task 2: Canonical config and public deployment contract

**Files:**
- Modify: `examples/oss-routing/vllm-semantic-router.yaml`
- Modify: `examples/oss-routing/README.md`
- Modify: `README.md`
- Test: `mesh/tests/test_vllm_semantic_router.py`

**Interfaces:**
- Consumes: vLLM Semantic Router v0.3 canonical schema and Mesh's OpenAI API on
  `127.0.0.1:8080`.
- Produces: caller endpoint `http://127.0.0.1:8888` and provider model
  `slancha-auto -> auto`.

- [ ] Add failing contract tests for loopback bindings, backend model mapping,
  exact version documentation, and direct-Mesh rollback.
- [ ] Run focused tests and confirm the new assertions fail against current
  optional-language documentation.
- [ ] Update the configuration and docs so vLLM Semantic Router is the normal
  front door and Mesh is explicitly internal transport.
- [ ] Validate with the released `vllm-sr==0.3.0` CLI.
- [ ] Run focused tests and Ruff, then commit.

### Task 3: Durable live cutover and rollback proof

**Files:**
- Modify: `SESSION_LOG_2026_08_01.md`
- Runtime-only: `~/.local/state/slancha-mesh/vllm-semantic-router/`
- Runtime-only: `~/Library/LaunchAgents/ai.slancha.vllm-semantic-router.plist`

**Interfaces:**
- Consumes: live Mesh endpoint `127.0.0.1:8080` and Docker Desktop.
- Produces: live vLLM front door `127.0.0.1:8888` supervised by launchd.

- [ ] Install the pinned CLI into the isolated runtime directory and validate
  the checked-in config.
- [ ] Start through the real launchd context and wait for `vllm-sr status` plus
  a real HTTP readiness probe.
- [ ] Send a unique-marker OpenAI chat request to port 8888 and confirm a fleet
  model returns the marker through Mesh.
- [ ] Stop the vLLM layer and prove the same request still succeeds directly on
  port 8080; restart the vLLM layer afterward.
- [ ] Inspect committed/runtime files and logs for secrets.
- [ ] Record exact version, caller, output, service state, and rollback evidence
  in the session log; commit.

### Task 4: Final gates and review

**Files:**
- Modify only files required by accepted BLOCKER or MAJOR findings.

**Interfaces:**
- Consumes: complete branch diff and live evidence.
- Produces: reviewed, pushed PR update with no merge.

- [ ] Run focused integration tests, full pytest, Ruff, package builds, package
  boundary checks, workflow parse, and GitNexus `detect-changes` against main.
- [ ] Send a bounded build-loop packet to an independent read-only reviewer.
- [ ] Fix confirmed BLOCKER/MAJOR findings with red-green regressions and rerun
  deterministic gates.
- [ ] Push the branch, watch GitHub Actions to completion, and leave the PR open.

