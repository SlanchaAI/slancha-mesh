# vLLM Semantic Router front door

vLLM Semantic Router is the supported caller-facing router. Slancha-Mesh stays
narrow behind it: discover local nodes, select a request-ready binding, proxy
to the fleet, and return a typed punt when local execution is not suitable.

## Semantic selection: vLLM Semantic Router

Run Slancha-Mesh internally on `127.0.0.1:8080`, then install, validate, and
serve the pinned vLLM Semantic Router v0.3.0 runtime:

```bash
slancha-mesh semantic-router install \
  --config examples/oss-routing/vllm-semantic-router.yaml
slancha-mesh semantic-router validate
slancha-mesh semantic-router serve
```

The lifecycle command sets `VLLM_SR_STACK_NAME=slancha-mesh` and
`VLLM_SR_PORT_OFFSET=100`; the config's internal listener `:8788` is therefore
published on host `:8888`, while vLLM's own control API no longer collides
with Mesh on `:8080`. Its Docker runtime reaches the host Mesh router through
`host.docker.internal:8080`; `localhost:8080` would
incorrectly refer to the router container. Send `model: MoM` to activate vLLM
Semantic Router's decision path. It maps `slancha-auto` to `model: auto` at the
Mesh boundary, where live fleet readiness makes the final node choice.

The lifecycle shim forces every port published by the upstream local runtime
onto host `127.0.0.1`, including Envoy, the router control ports, Redis,
Postgres, and the simulator. Container listeners still bind their container
interfaces so Docker can forward traffic; no vLLM runtime port is exposed on
the LAN merely because the canonical config listens on `0.0.0.0` internally.

This alpha keeps image, audio, transcription, edit, and video clients off the
semantic front door: call Mesh `:8080` directly for those routes. See the
[`examples/multimodal`](../multimodal/README.md) adapters. Chat stays on
`:8888`; cloud handoff after any typed punt remains a caller-owned Barkeep or
equivalent policy decision.

This profile uses vLLM's supported static selector because it exposes one
dynamic Mesh backend. vLLM's learned selectors apply when a decision contains
multiple model candidates; duplicating Mesh's changing node/model catalog in a
static vLLM config would make readiness stale. The profile also disables the
unused semantic cache and tools index, avoiding their model and storage stack.

Install it durably after the first successful foreground request:

```bash
slancha-mesh service install --kind semantic-router
```

The service runs a state-aware supervisor. Healthy containers stay untouched;
missing router or Envoy containers are recreated after Docker becomes
available. Full recreation can take roughly two minutes on this Mac because
the upstream runtime reprovisions both request-path containers.

Rollback never touches a model node: stop the semantic layer and point the
caller back to `http://127.0.0.1:8080`.

## Cloud execution after a punt

Inference Gateway is the lean preferred external executor; LiteLLM remains an
option when its broader provider surface is worth the dependency and licensing
tradeoff. Neither is embedded in Slancha-Mesh.

The caller must inspect the local response before making a separate request:

```python
local = client.post("http://127.0.0.1:8888/v1/chat/completions", json=request)
if local.headers.get("X-Slancha-Outcome") == "punt":
    # Apply user consent, budget, and egress policy here.
    cloud = client.post("http://127.0.0.1:8081/v1/chat/completions", json=request)
```

Do not configure blind retry from Slancha to a paid endpoint. A 503 without
`X-Slancha-Outcome: punt` is an ordinary transport or service failure.

For a local, no-spend compatibility probe, copy
[`inference-gateway.env.example`](inference-gateway.env.example), point
`OLLAMA_API_URL` at an OpenAI-compatible local endpoint, export the values, and
start the `inference-gateway` binary. Prefix the model with `ollama/` when
calling the gateway.

## Node process management: llama-swap

llama-swap can manage several model-server processes on one constrained node.
Point a Slancha specialist card's `node_url` at llama-swap's OpenAI-compatible
listener. Slancha continues to select the node; llama-swap owns loading and
unloading model processes on that node. Keep node-info `/models` truthful:
advertise only model identifiers llama-swap can start.

## Ownership map

| Layer | Owner |
|---|---|
| Fleet discovery, local failover, runtime circuits | Slancha-Mesh |
| Caller-facing API, semantic policy, and model intent | vLLM Semantic Router v0.3.0 |
| Paid provider choice, credentials, execution | Caller + Inference Gateway (optional) |
| Per-node model process swapping | llama-swap (optional) |
| Subscription/free/paid lane policy | Barkeep or another caller policy layer |

Research and version notes live in
[`docs/research/oss-routing-2026-08-01/ATLAS.md`](../../docs/research/oss-routing-2026-08-01/ATLAS.md).
