# Inference Gateway

- Source: https://github.com/inference-gateway/inference-gateway
- Crawled: 2026-08-01
- Status: ACTIVE
- License: Apache-2.0
- Evidence read: README, provider and routing source, release metadata,
  configuration docs; checksum-verified v0.44.0 arm64 binary and CLI probe.

## Inventory and Architecture

Inference Gateway is a Go gateway exposing OpenAI-compatible APIs across OpenAI,
Anthropic, DeepSeek, Google, Groq, Mistral, Moonshot, NVIDIA, Ollama, and other
providers. It includes authentication options, Open Policy Agent integration,
OpenTelemetry, Model Context Protocol support, and opt-in logical aliases/pools.
Its pool selection is static/configured rather than tailnet-discovered.

## Surprises and Decision

The current binary is operationally lean despite broad provider support. It does
not solve semantic quality selection or changing fleet discovery, which is a
useful boundary: run it only when a caller authorizes a typed Slancha punt. It is
the preferred initial cloud-execution example because it avoids embedding a
provider SDK in Slancha core.

**Trust:** [P, upstream source + checksum-verified binary probe, 94/100].
