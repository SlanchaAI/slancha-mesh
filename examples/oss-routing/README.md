# Composing with open-source routers

Slancha-Mesh stays narrow: it discovers local nodes, serves models, selects a
request-ready local binding, and returns a typed punt when local execution is
not suitable. Use existing open-source projects for adjacent layers.

## Semantic selection: vLLM Semantic Router

Run Slancha-Mesh on `127.0.0.1:8080`, then validate and serve
[`vllm-semantic-router.yaml`](vllm-semantic-router.yaml) with vLLM Semantic
Router v0.3:

```bash
vllm-sr validate --config examples/oss-routing/vllm-semantic-router.yaml
vllm-sr serve --config examples/oss-routing/vllm-semantic-router.yaml
```

The semantic router listens on `:8899` and treats the entire mesh as one
OpenAI-compatible backend. Keep `model: auto` if Slancha should make the final
specialist choice; name a Slancha specialist directly when the outer selector
already made that decision.

## Cloud execution after a punt

Inference Gateway is the lean preferred external executor; LiteLLM remains an
option when its broader provider surface is worth the dependency and licensing
tradeoff. Neither is embedded in Slancha-Mesh.

The caller must inspect the local response before making a separate request:

```python
local = client.post("http://127.0.0.1:8080/v1/chat/completions", json=request)
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
| Semantic policy and model intent | vLLM Semantic Router (optional) |
| Paid provider choice, credentials, execution | Caller + Inference Gateway (optional) |
| Per-node model process swapping | llama-swap (optional) |
| Subscription/free/paid lane policy | Barkeep or another caller policy layer |

Research and version notes live in
[`docs/research/oss-routing-2026-08-01/ATLAS.md`](../../docs/research/oss-routing-2026-08-01/ATLAS.md).
