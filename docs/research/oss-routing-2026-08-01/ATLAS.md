# OSS Routing Atlas — Hybrid Local and Cloud Inference

**Crawled:** 2026-08-01  
**Decision scope:** Slancha-Mesh core and optional routing integrations  
**Primary question:** Which open-source systems should Slancha-Mesh reuse for model selection, local serving, load balancing, and cloud escalation?

## Scope and Method

The concrete problem is a seven-box personal fleet: discover changing local model
servers, route work to a healthy weak-enough model, and hand unsuitable work to a
larger cloud model without bypassing Barkeep's spend policy. The generalized
problem separates five planes: discovery, selection, transport, provider
translation, and policy.

Search axes covered semantic routers, static API gateways, Kubernetes inference
control planes, node process managers, and multi-provider SDKs. A recent-community
scan produced mostly unrelated results, so adoption claims below rely on upstream
repositories, releases, source, and clean binary/package probes. Candidate counts
and popularity are discovery signals, not quality evidence.

Review personas:

- personal-fleet operator: low idle cost, simple recovery, LAN and tailnet fit;
- inference engineer: request semantics, streaming, failure handling, observability;
- OSS maintainer: license clarity, dependency surface, release hygiene;
- spend-policy reviewer: no implicit paid call, isolated credentials, visible handoff;
- completeness critic: missing categories, counterexamples, and integration cost.

## Decision

No reviewed project replaces Slancha-Mesh's pull discovery of tagged tailnet nodes
and host-pinned local routing. Slancha should keep that narrow capability and
compose mature OSS systems at OpenAI-compatible boundaries.

1. **Adopt the vLLM Semantic Router protocol boundary, not its runtime, in core.**
   It is the strongest reviewed smart-selection substrate, but its normal runtime
   includes Envoy, containers, native components, model assets, and a dashboard.
   Operators may place it in front of Slancha for semantic selection.
2. **Return a typed punt from core.** A punt is a stable, machine-readable
   request for the caller to choose an escalation path. Core will not choose a
   cloud vendor, read provider keys, or silently spend.
3. **Use an external OSS gateway to execute cloud calls.** Inference Gateway is
   the preferred lean provider-normalization option. LiteLLM remains the broadest
   compatibility option, with a larger dependency and mixed-license surface.
   OpenZiti llm-gateway is a promising all-in-one option, but its static discovery
   and overlay duplicate Slancha/Tailscale responsibilities.
4. **Treat llama-swap as a node adapter.** It complements specialist nodes by
   managing local processes and model swaps; it is not a fleet or cloud router.
5. **Do not adopt a Kubernetes control plane for the personal fleet.** Gateway API
   Inference Extension, llm-d, AIBrix, Dynamo, and vLLM Production Stack solve a
   larger orchestration problem and would make the base operationally heavier.

This yields one explicit flow:

```text
client / barkeep
    -> optional vLLM Semantic Router (semantic selection)
    -> slancha-mesh (dynamic local discovery + transport)
    -> typed punt on no suitable/healthy local route
    -> caller-approved Inference Gateway / LiteLLM / llm-gateway (cloud execution)
```

The typed punt preserves `BARKEEP_NO_SPEND`: Slancha reports inability; the caller
that owns quota and provider policy decides whether to spend.

## Capability Matrix

| Project | Best role | Dynamic fleet discovery | Smart selection | Provider translation | Base-fit verdict |
|---|---|---:|---:|---:|---|
| Slancha-Mesh | tailnet discovery + local transport | yes | limited | no | keep narrow core |
| vLLM Semantic Router | semantic selection/control plane | no | strong | partial | optional outer router |
| Inference Gateway | provider gateway | no | basic pools | strong | preferred escalation executor |
| OpenZiti llm-gateway | semantic gateway + overlay | static endpoints | medium | medium | reference/optional sidecar |
| LiteLLM | broad provider SDK/proxy | static deployments | many strategies | strongest breadth | optional, pin/audit surface |
| llama-swap | node process/model manager | no | no | no | optional node adapter |

## Candidate Index

Nineteen candidates were screened. Five received source-and-runtime dossiers.

| Candidate | Category | Disposition | Reason |
|---|---|---|---|
| vLLM Semantic Router | semantic router | deep-crawl; integrate | strongest modular selection model |
| Inference Gateway | provider gateway | deep-crawl; integrate | lean binary and broad providers |
| OpenZiti llm-gateway | gateway/router | deep-crawl; watch | good features, young and overlapping overlay |
| LiteLLM | SDK/proxy | deep-crawl; optional | broadest compatibility; heavy/mixed surface |
| llama-swap | node manager | deep-crawl; integrate | excellent local-server complement |
| Bifrost | provider gateway | watch | fast active gateway; duplicates executor role |
| Plano (formerly Arch) | agentic gateway | watch | broader agent/tool focus than fleet routing |
| Envoy AI Gateway | gateway | watch | strong data plane; Kubernetes-oriented control |
| Gateway API Inference Extension | Kubernetes routing | defer | excellent standardized signals; cluster cost |
| llm-d | distributed inference | defer | Kubernetes-native serving stack |
| vLLM Production Stack | serving stack | defer | Kubernetes deployment scope |
| NVIDIA Dynamo | distributed inference | defer | datacenter GPU runtime, excessive for base |
| AIBrix | inference control plane | defer | Kubernetes control-plane scope |
| SGLang router | serving router | watch | tied to SGLang serving topology |
| LocalAI | local runtime | watch | broader server; overlaps engines, not discovery |
| GPUStack | model platform | defer | platform scope exceeds node adapter |
| Portkey Gateway | provider gateway | watch | executor alternative; no tailnet discovery |
| RouteLLM | research router | do not adopt | useful weakest-sufficient idea, inactive runtime |
| TensorZero | optimization gateway | do not adopt | upstream archived/defunct in 2026 |

Machine-readable details live in `index.jsonl`. Full findings for the five
deep-crawled projects live in `dossiers/`.

## Reusable Findings

### F1 — Separate route signals from route decisions

vLLM Semantic Router v0.3 models signals, projections, decisions, algorithms,
and model targets as distinct configuration layers. Slancha should expose stable
capabilities and outcomes so an external router can decide without importing
Slancha internals. **Trust:** [P, upstream source/release, 92/100].

### F2 — Use passive request failures as health evidence

OpenZiti llm-gateway combines active endpoint checks with passive failover.
Slancha currently proves metadata reachability, not inference readiness. Record
bounded request outcomes and stop advertising an open-circuit binding as
routable. **Trust:** [P, upstream source and config docs, 90/100].

### F3 — Provider translation is already a mature OSS layer

Inference Gateway and LiteLLM normalize many providers behind OpenAI-compatible
interfaces. Implementing provider-specific request translation in Slancha would
duplicate fast-moving work. **Trust:** [P, upstream source/docs, 95/100].

### F4 — Local model lifecycle belongs at the node

llama-swap owns model processes, automatic swapping, profiles, logs, and metrics
behind OpenAI/Anthropic-compatible endpoints. Slancha can discover that endpoint
without taking ownership of model lifecycle. **Trust:** [P, upstream source and
release binary, 91/100].

### F5 — Explicit escalation beats silent fallback

Gateways make retries and fallbacks convenient, but the personal fleet has a
separate spend authority in Barkeep. A typed punt preserves that authority and
keeps paid execution observable. This is an architecture inference from the
reviewed projects and local system boundaries. **Trust:** [S, cross-project
synthesis plus local source, 88/100].

## Follow-through — STATUS: ✅ SHIPPED

| Rank | Finding | Delivery target | Acceptance criterion | Status |
|---:|---|---|---|---|
| 1 | F5 typed punt | core router | real HTTP request returns stable punt body + headers; no upstream call | shipped — real-TCP test |
| 2 | F2 passive health | core router `/health` | failed live binding becomes unroutable and later recovers | shipped — open/suppress/half-open proof |
| 3 | F1 router compatibility | docs + contract tests | vLLM SR-compatible OpenAI model list/chat flow passes | shipped — v0.3 config validated by released CLI |
| 4 | F3 gateway composition | example + live probe | punt consumer reaches a local OSS gateway without provider spend | shipped — released Inference Gateway binary reached local stub |
| 5 | F4 node adapter | node docs | llama-swap endpoint is represented without core dependency | shipped — documented OpenAI endpoint seam |

Evidence on 2026-08-01:

- `mesh/tests/test_router_live_socket.py` opened real TCP listeners, returned a
  local completion, opened the circuit after two upstream 503s, suppressed the
  next call with `local_attempts=0`, then recovered through one half-open probe.
- `vllm-sr validate --config
  examples/oss-routing/vllm-semantic-router.yaml` passed against vLLM Semantic
  Router v0.3.
- Inference Gateway's released binary, configured only with a local
  `OLLAMA_API_URL`, listed `ollama/test-large` and returned `GATEWAY-OK` from a
  local OpenAI-compatible stub. No provider credential or external request was
  used.

## Primary Sources

- [vLLM Semantic Router](https://github.com/vllm-project/semantic-router)
- [Inference Gateway](https://github.com/inference-gateway/inference-gateway)
- [OpenZiti llm-gateway](https://github.com/openziti/llm-gateway)
- [LiteLLM](https://github.com/BerriAI/litellm)
- [llama-swap](https://github.com/mostlygeek/llama-swap)
- [Kubernetes Gateway API Inference Extension](https://github.com/kubernetes-sigs/gateway-api-inference-extension)
- [llm-d](https://github.com/llm-d/llm-d)
