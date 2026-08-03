# OSS-Native Routing Integration Plan

**Goal:** Keep Slancha-Mesh a lean dynamic local-fleet layer, make failed or
unsuitable local routing truthful, compose with current OSS routers/gateways,
and split fine-tuning into an optional distribution.

**Architecture:** Slancha owns tailnet/LAN discovery, local binding selection,
transport, runtime health, and typed punts. vLLM Semantic Router may run in
front for semantic selection. A caller-selected OSS gateway may handle a punt
after Barkeep authorizes spend. Core never imports those runtimes or executes a
cloud request.

## Constraints

- Preserve explicit specialist IDs, `model: "auto"`, LAN/tailnet discovery,
  `specialists_reachable`, and usage telemetry from `origin/main`.
- Keep the base dependency set free of provider SDKs, router containers, model
  assets, and training libraries.
- A punt uses HTTP 503 plus `X-Slancha-Outcome: punt` and
  `X-Slancha-Reason`; the OpenAI-style body has stable type/code/details.
- Runtime health stores bounded failure metadata, never prompts or responses.
- No core path reads cloud credentials or makes a paid-provider request.
- Every new route must have a live caller before completion.

## Task 1 — Integrate the default branch

1. Merge `origin/main` without rewriting LAN/Qwen commits.
2. Re-index GitNexus and run the full baseline.
3. Verify usage telemetry, LAN advertise-host, and router tests.

## Task 2 — Add the typed punt contract

1. Write pure tests for the stable payload and headers.
2. Add `mesh/escalation.py` containing punt reason codes and response builders.
3. Change auto-route and local-exhaustion paths to return the contract.
4. Prove with a real HTTP socket request and assert no second upstream call.

## Task 3 — Add passive runtime health

1. Write deterministic circuit-state tests using an injected clock.
2. Add a bounded per-binding state machine.
3. Record transport timeout/retriable 5xx/success in router request paths.
4. Add `specialists_routable`, circuit/counter fields, and degraded reasons to
   `/health` while retaining `specialists_reachable`.
5. Prove a failed live binding disappears from routable capacity and recovers
   after a successful half-open request.

## Task 4 — Prove OSS compatibility

1. Add a contract test showing an OpenAI-compatible outer router can list
   models and route explicit model requests through Slancha.
2. Add checked examples for vLLM Semantic Router in front, Inference Gateway
   after a punt, and llama-swap at a node.
3. Run a local OSS gateway binary or faithful no-spend local configuration in
   the socket proof. Record the exact producer/caller chain.

## Task 5 — Split tuning from core

1. Create a uv workspace with `slancha-mesh` and `slancha-mesh-tune` build
   roots; use explicit artifact include/exclude lists.
2. Move training, replay, corpus, evaluation, and experiment-loop code into the
   add-on. Keep core imports one-way and delete the unwired core `loop` command.
3. Preserve public core imports only where a known caller exists.
4. Build both artifacts; test a clean core-only install and a clean combined
   install. Core artifacts must contain no tuning code, corpora, tests, or
   legacy dashboard.

## Task 6 — Personal-fleet and OSS readiness

1. Add real launchd router and systemd node service roles; render first, then
   install only after resolving exact existing job/unit state.
2. Prefer `specialists_routable` in Barkeep's mesh tap with backward
   compatibility; keep fleet dashboard read-only.
3. Correct README installation/runtime claims and add SECURITY, CONTRIBUTING,
   changelog/release guidance, package metadata, and source-build instructions.
4. Inspect wheels/sdists and run strict catalog validation, Ruff, and the full
   suite.

## Completion Gate

- Core-only clean install serves, discovers, routes, and punts with no tuning
  import or cloud call.
- Add-on clean install exposes only wired tuning/evaluation commands.
- Real socket proofs cover local success, typed punt, circuit suppression and
  recovery, plus one OSS compatibility path.
- Barkeep observes routable capacity and preserves spend vetoes.
- Node and router survive process termination under their service managers, or
  are reported as `built, not wired` with the exact external blocker.
- Fresh semantic review has no open BLOCKER; all focused/full checks pass.
