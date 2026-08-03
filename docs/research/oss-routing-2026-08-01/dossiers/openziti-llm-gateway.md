# OpenZiti llm-gateway

- Source: https://github.com/openziti/llm-gateway
- Crawled: 2026-08-01
- Status: ACTIVE (YOUNG)
- License: Apache-2.0
- Evidence read: README, config examples, router/provider/health source,
  SECURITY, CONTRIBUTING, CHANGELOG, releases; checksum-verified v0.1.5 arm64
  binary and CLI probe.

## Inventory and Architecture

The single Go binary exposes OpenAI-compatible traffic, supports OpenAI,
Anthropic, and generic local endpoints, and provides weighted round-robin,
active health checks, passive failover, streaming, metrics, virtual keys, and a
heuristic/embedding/classifier semantic cascade. OpenZiti/zrok supplies an
optional identity overlay.

## Surprises and Decision

Engineering hygiene is strong for a young project: checksums, Software Bill of
Materials, security policy, and release artifacts. Its unreleased changelog also
records recent fixes for consumed request bodies during failover, swallowed
streaming errors, and ignored configuration fields. Static endpoints and the
overlay duplicate Slancha's discovery and Tailscale layer. Reuse the active plus
passive health pattern; keep the gateway external while it matures.

**Trust:** [P, upstream source + checksum-verified binary probe, 92/100].
