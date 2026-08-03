# vLLM Semantic Router Front-Door Design

## Decision

Make vLLM Semantic Router v0.3.0 the default caller-facing router for the
personal fleet and the recommended OSS deployment. Keep Slancha Mesh behind it
as a private OpenAI-compatible backend responsible for tailnet discovery,
request-ready node selection, circuit state, and transport.

The live request path becomes:

```text
caller -> vLLM Semantic Router :8888 -> Slancha Mesh :8080 -> fleet model
                                      \-> configured cloud provider later
```

The cloud branch remains disabled during this cutover. Enabling it requires a
separate provider, credential, egress, and spend-policy decision. Until then,
Slancha's typed punt remains the fail-closed escalation contract.

## Why this boundary

vLLM Semantic Router owns semantic signals, decisions, model cards, policy,
observability, and the OpenAI-compatible front door. Slancha Mesh owns the
private-fleet capability vLLM Semantic Router does not provide: pull discovery
over the tailnet plus live node-level failover. This removes custom semantic
routing from the product direction without discarding working fleet transport.

Rejected alternatives:

1. Embed or fork vLLM Semantic Router inside the Python package. This creates a
   large maintenance surface and defeats the adopt-over-build goal.
2. Remove Slancha's router process immediately and point vLLM Semantic Router
   directly at every node. Its static configuration would lose pull discovery
   and require config regeneration whenever fleet membership changes.
3. Keep vLLM Semantic Router as documentation only. That leaves the hand-built
   router as the real front door and does not satisfy the requested cutover.

## Version and runtime contract

- Pin stable vLLM Semantic Router `v0.3.0` and its immutable container tags.
- Run its supported local Docker runtime on macOS.
- Bind the vLLM front door to `127.0.0.1:8888`.
- Keep Slancha Mesh bound to `127.0.0.1:8080`.
- Preserve `ai.slancha.mesh-router` as an independently durable launchd job.
- Add an independently removable vLLM Semantic Router launchd job.
- Store generated runtime state outside the repository under
  `~/.local/state/slancha-mesh/vllm-semantic-router`.
- Do not place secrets in configuration, plist files, logs, or the repository.

## Routing behavior

The first live configuration exposes a single `slancha-auto` model backed by
Slancha's `model: auto`. This makes vLLM Semantic Router the active front door
without pretending one local model constitutes meaningful model-choice
optimization. The configuration remains canonical v0.3 YAML so additional
local or cloud model cards and semantic decisions can be added without changing
Slancha Mesh.

Requests addressed to the front door must preserve OpenAI request and response
shape. Slancha's typed punt headers and body must survive the proxy path. If the
vLLM layer is unavailable, rollback consists of restoring the caller endpoint
to `http://127.0.0.1:8080`; no mesh node or model process changes.

## Repository surface

- Promote `examples/oss-routing/vllm-semantic-router.yaml` from an optional
  example to the supported front-door configuration.
- Add a version-pinned installer/launcher that uses an isolated environment,
  validates the config, and never pipes a remote script into a shell.
- Add deterministic tests for configuration ownership, pinning, command
  rendering, and rollback instructions.
- Update README and the OSS composition guide so `:8888` is the normal caller
  endpoint and `:8080` is the internal mesh endpoint.
- Record live commands, versions, request evidence, and rollback in the session
  log.

## Success criteria

1. Stable vLLM Semantic Router v0.3.0 installs from a pinned release artifact.
2. The checked-in v0.3 configuration validates with the released CLI.
3. A durable local runtime starts without replacing the Mesh launchd job.
4. A real OpenAI chat request enters `127.0.0.1:8888`, traverses
   `127.0.0.1:8080`, and returns content from a fleet model.
5. Logs or headers identify both routing layers in the observed path.
6. Stopping vLLM Semantic Router leaves direct Mesh routing on `:8080` intact.
7. Focused tests, full suite, Ruff, packaging, and GitHub Actions pass.
8. No cloud credential is read and no paid request is issued.
