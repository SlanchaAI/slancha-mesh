# vLLM Semantic Router

- Source: https://github.com/vllm-project/semantic-router
- Crawled: 2026-08-01
- Status: ACTIVE
- License: Apache-2.0
- Evidence read: README, v0.3 release/config docs, CLI package, example recipes,
  runtime source; clean Python 3.12 wheel install and CLI probe.

## Inventory and Architecture

The router accepts OpenAI-compatible traffic and decomposes selection into
signals, projections, decisions, algorithms, and backend models. The v0.3
Themis configuration adds stateful/session-aware decisions, replay, protocol
translation, readiness, metrics, and dashboard tooling. Backend references can
target ordinary HTTP endpoints, so Slancha can appear as one model-serving
backend.

The lightweight Python CLI installed cleanly with 25 packages. Normal `serve`
operation launches a heavier system: container runtime, Envoy, native Go/Rust
components, router models/assets, dashboard, and optional simulator. The source
tree also carries training machinery.

## Surprises and Decision

The configuration architecture is more reusable than the runtime dependency.
Importing it into base would replace a small mesh service with a control plane.
Use it as an optional outer router over Slancha's OpenAI-compatible surface.
Adopt its separation of signals from decisions and prove protocol compatibility.

**Trust:** [P, upstream source + clean install probe, 94/100].

