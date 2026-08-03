# LiteLLM

- Source: https://github.com/BerriAI/litellm
- Crawled: 2026-08-01
- Status: ACTIVE
- License: MIT core; separately licensed `enterprise/` tree
- Evidence read: README, root and enterprise licenses, `pyproject.toml`, router
  source, proxy extras, release metadata.

## Inventory and Architecture

LiteLLM provides the broadest reviewed provider normalization, plus an SDK router
and a full proxy with retries, fallbacks, load balancing, cost accounting,
virtual keys, observability, and a management plane. The base SDK dependency set
is moderate; the proxy extra is much larger and includes separately versioned
proxy/enterprise packages and database/UI services.

## Surprises and Decision

The repository's root MIT license explicitly excludes the enterprise directory,
and several proxy features cross that product boundary. This does not prevent
using the OSS SDK/proxy, but it makes an unqualified base dependency a poor fit
for an OSS project that wants a small, obvious license surface. Use a pinned,
external deployment only when its provider breadth is required; never install
the full proxy extra with Slancha core.

**Trust:** [P, upstream licenses + package metadata/source, 93/100].
