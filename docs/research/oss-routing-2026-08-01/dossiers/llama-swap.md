# llama-swap

- Source: https://github.com/mostlygeek/llama-swap
- Crawled: 2026-08-01
- Status: ACTIVE
- License: MIT
- Evidence read: README, configuration and process-management source, release
  metadata; checksum-verified v245 arm64 binary and CLI probe.

## Inventory and Architecture

llama-swap starts and stops local model-server processes on demand behind
OpenAI- and Anthropic-compatible APIs. It supports model groups, profiles,
matrices, logs, metrics, config reload, and automatic model swapping. It owns
the node lifecycle but has no dynamic fleet discovery or cloud-provider policy.

## Surprises and Decision

Its narrow responsibility is complementary: a Slancha node can advertise a
llama-swap endpoint exactly as it advertises Ollama or vLLM. Document the adapter
and keep process lifecycle out of core. A future catalog field may declare
llama-swap startup metadata, but current generic adopted endpoints are enough.

**Trust:** [P, upstream source + checksum-verified binary probe, 93/100].
