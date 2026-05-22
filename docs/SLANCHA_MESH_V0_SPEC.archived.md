# Slancha-Mesh v0 — Spec [ARCHIVED]

> ⚠️ **SUPERSEDED** 2026-05-22 by [`SLANCHA_PROTOCOL_v0.1_DRAFT.md`](./SLANCHA_PROTOCOL_v0.1_DRAFT.md) (tagged `v0.1.0-spec`).
>
> The successor spec inverts the architecture: mesh = placement substrate + signal-rich protocol (not classifier-owning router). Edge-side L@E origin-request routing, signed `did:web` cards, multi-axis agent preferences, OpenAI + Anthropic endpoint shapes, license bifurcation (Apache spec / AGPL ref impl).
>
> This document retained for historical context only. **Do not implement against this spec — implement against v0.1.0-spec.**

---

> **Original status**: Draft, 2026-05-15. Author: slancha-local Claude session.
> **Sister doc**: `EXO_SPARK_PROBE_2026_05_15.md` (exo cluster prototype on 2 Sparks).
> **Provenance**: distilled from exolabs.net days 1–12, `~/Source/exo/docs/{architecture.md,api.md}`,
> exo placement source (`src/exo/master/placement.py`), and replay-router eval
> (`scripts/replay_router.py`, `docs/ROUTER_IDEAS_REPLAY_2026_05_12.md`).

---

## 1. What Slancha-Mesh is

A **mesh of specialist small models** running on the user's own hardware,
fronted by Slancha's classifier-driven router. Each node hosts whole models
(no sharding); the router picks `(specialist, node)` per request. Cloud is
the escalation tier, not the default.

This is fundamentally a different workload than exo's flagship.

| Dimension | exo | Slancha-Mesh |
|---|---|---|
| **Workload** | one big model, pipeline/tensor parallel across N nodes | many small specialists, each whole on one node |
| **Bottleneck** | inter-device activations latency (network-bound) | classifier accuracy + node utilization (CPU/GPU-bound) |
| **Primary metric** | tokens/sec for a single big model | accuracy-per-dollar across heterogeneous specialists |
| **Network topology matters because** | activations shipped every layer boundary | only model swap + heartbeats; routing is request-level |
| **Hardware story** | "let me run DeepSeek 671B on consumer Macs" | "your hardware specializes into a personal expert panel" |

We borrow exo's substrate (libp2p discovery, event sourcing, model-card TOML,
API surface) and **replace** the inference layer with whole-model serving via
vLLM / llama.cpp / Ollama / cloud, picked per-request by Slancha's existing
classifier.

## 2. Architecture overview

```
┌─ Client (any OpenAI/Claude/Ollama-compatible app) ─────────────────┐
│                                                                    │
│                              ▼                                     │
│         slancha-router (slancha-api or local proxy)                │
│         │                                                          │
│         │  1. classify(prompt) → signals                           │
│         │     domain, difficulty, language, tool_use, jailbreak    │
│         │  2. registry.lookup(signals) → ranked (specialist,node)  │
│         │  3. health-check + queue-aware pick                      │
│         │  4. forward request                                      │
│         ▼                                                          │
│   ┌────────────┬────────────┬────────────┬────────────┐            │
│   │ Spark-1    │ Spark-2    │ Mac mini   │ RTX box    │ cloud      │
│   │  vLLM      │  vLLM      │  llama.cpp │  vLLM/SGLang│ slancha-api │
│   │            │            │            │            │            │
│   │ - qwen-    │ - qwen-    │ - aya-     │ - qwen-    │ - opus-4-7 │
│   │   math-7B  │   coder-7B │   8B-multi │   VL-2B    │ - gpt-5.5  │
│   │ - llama-3- │ - phi-4-14B│ - whisper- │ - flux-1   │ - sonnet   │
│   │   8B (gen) │            │   large    │   (image)  │            │
│   └────────────┴────────────┴────────────┴────────────┘            │
│         ▲                                                          │
│         │                                                          │
│   ┌─ slancha-registry (event-sourced, on slancha-api) ─┐           │
│   │  - heartbeats (every 5s)                           │           │
│   │  - hardware snapshots (VRAM, bandwidth, util)      │           │
│   │  - loaded specialists per node                     │           │
│   │  - bandwidth probes (1/min)                        │           │
│   └────────────────────────────────────────────────────┘           │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

Three things make this different from "round-robin load balancer for LLMs":

1. **Specialist allocator** decides which models each node should host based
   on its hardware and the cluster's coverage gaps. Run once at boot,
   re-run on cluster topology change.
2. **Classifier-driven routing** uses domain/difficulty/language signals
   (already in slancha-api) to pick the right specialist, not just the
   first available node.
3. **Idle-fine-tune daemon** detects sub-10%-util windows and runs LoRA
   updates on the loaded specialist using recent traffic. This is the
   compounding moat. Borrowed conceptually from exo's day 5 (DiLoCo) and
   day 12 (SPARTA): low-bandwidth distributed training is now feasible.

## 3. Hardware-aware suggester (the new bit)

**Goal**: every node, on boot, looks at its hardware + current network
position + cluster's existing coverage and **suggests a model to host**.
Or: refuses to host anything if the cluster is already balanced and this
node would just be a duplicate.

### 3.1 Inputs (per-node probe)

```python
@dataclass(frozen=True)
class NodeProbe:
    node_id: NodeId
    friendly_name: str          # "promaxgb10-d325"
    # Compute
    chip: str                   # "NVIDIA GB10", "Apple M5", "AMD Ryzen 9", "Intel i9"
    arch: Literal["aarch64", "x86_64", "apple-silicon"]
    cuda_capability: str | None # "12.0" (Blackwell), "9.0" (Hopper), None
    fp4_tops: float | None      # GB10 ≈ 7600 sparse, 3800 dense at FP4
    fp16_tops: float | None     # for non-FP4 hardware
    # Memory
    ram_total_gb: float
    ram_available_gb: float
    vram_total_gb: float | None # for discrete GPUs; None for unified-mem
    vram_available_gb: float | None
    unified_memory: bool        # True for Apple Silicon, Spark GB10
    memory_bandwidth_gbs: float # peak read bandwidth, used to estimate token rate
    # Network
    public_ipv4: str | None
    lan_interfaces: list[str]   # ["enp1s0f1np1", "wlP9s9", "tailscale0"]
    bandwidth_to_master_mbps: float  # measured at boot
    rtt_to_master_ms: float
    thunderbolt5: bool          # exo's day-1 killer feature; not relevant for us yet
    # Backend availability
    available_backends: list[Literal["vllm", "llamacpp", "ollama", "mlx"]]
    # Storage
    disk_free_gb: float
```

`chip` and `arch` come from `lscpu` + `nvidia-smi` / `system_profiler`.
`fp4_tops` is looked up against a table we maintain (Blackwell GB10, Hopper
H100/H200, Ada L4/L40, Apple M-series, AMD MI-series). `memory_bandwidth_gbs`
is critical — token decode rate is roughly `bandwidth_gbs / model_size_gb`,
so a 121GB-unified-mem-at-273GB/s Spark decodes a 7B Q4 model at
`273 / 4.2 ≈ 65 tok/s`. We use this as a first-pass throughput estimate
before any real benchmark.

### 3.2 Model fit scorer

Per `(specialist, node)` pair, compute a fitness score. The model card
gives us specialist metadata; the node probe gives us hardware; the result
is a single scalar.

```python
def model_fit_score(
    spec: SpecialistCard,
    node: NodeProbe,
    cluster_coverage: dict[DomainId, set[NodeId]],
) -> float:
    # Hard filters — return -inf if any fail
    if spec.required_backend not in node.available_backends:
        return -math.inf
    if spec.storage_gb > node.disk_free_gb:
        return -math.inf
    if spec.min_vram_gb > (node.vram_available_gb if not node.unified_memory else node.ram_available_gb):
        return -math.inf
    if spec.requires_fp4 and node.cuda_capability not in {"12.0", "10.0"}:
        return -math.inf

    # Soft scores
    headroom_gb = (node.vram_available_gb or node.ram_available_gb) - spec.runtime_gb
    headroom_score = min(headroom_gb / spec.runtime_gb, 2.0)  # cap at 2x headroom

    # Throughput estimate (memory-bandwidth bound for decode)
    est_tok_per_s = node.memory_bandwidth_gbs / spec.runtime_gb
    throughput_score = math.log(max(est_tok_per_s, 1)) / math.log(50)  # 50 t/s = 1.0

    # Coverage need — heavily reward hosting a specialist the cluster lacks
    nodes_already_hosting = len(cluster_coverage.get(spec.domain, set()))
    coverage_score = 3.0 if nodes_already_hosting == 0 else (1.0 / (1 + nodes_already_hosting))

    # Network position (closer to master = better for hot-path routing)
    network_score = max(0.5, 1.0 - node.rtt_to_master_ms / 100.0)

    return (
        2.0 * coverage_score
        + 1.5 * throughput_score
        + 0.5 * headroom_score
        + 0.3 * network_score
    )
```

`SpecialistCard` extends exo's model card (`resources/inference_model_cards/*.toml`)
with Slancha-specific fields:

```toml
# resources/specialists/qwen3-math-7b-q4.toml
model_id = "Qwen/Qwen3-Math-7B-Instruct-Q4_K_M"
domain = "math"
difficulty_tiers = ["medium", "hard"]
languages = ["en"]
required_backend = "vllm"   # or "llamacpp", "ollama"
requires_fp4 = false
storage_gb = 4.2
runtime_gb = 6.5            # weights + KV cache + overhead at typical ctx
min_vram_gb = 7.0
context_window = 32768
n_layers = 28
hidden_size = 3584
estimated_tps_at = { gb10 = 65, m4_pro = 55, m3_ultra = 90, l40 = 110 }
supports_lora_finetune = true
upstream_model_card = "mlx-community/Qwen3-Math-7B-Instruct-4bit"
```

### 3.3 Output (the suggestion)

```python
@dataclass(frozen=True)
class NodeSuggestion:
    node_id: NodeId
    primary: SpecialistCard           # the one model this node should always have loaded
    alternates: list[SpecialistCard]  # ranked by fit, if primary fails to load
    rationale: str                    # human-readable explanation
    sticky: bool                      # if True, allocator should NOT migrate this off
```

A node accepts the suggestion and starts pulling weights via the
backend's `huggingface-cli download` or equivalent. While downloading,
it streams progress to the registry so the allocator knows when capacity
becomes live.

### 3.4 Re-suggestion triggers

Cluster topology is not static. Re-run the allocator when:

- A new node joins (it gets a suggestion; existing nodes may shift).
- A node leaves cleanly (graceful) — others may need to absorb its specialty.
- A node has been unreachable for >5 min (treat as left).
- Traffic distribution shifts (e.g., suddenly 60% coding queries, more
  coding capacity needed — allocator may promote a `coding` alternate to
  `primary` on a node that was hosting `multilingual`).

## 4. Cluster coverage strategy — best-per-machine vs full-set

Two pure strategies and one hybrid. The allocator chooses based on
**cluster size** and **traffic shape**.

### Strategy A — "best per machine"

Each node picks the model where it's individually strongest, regardless of
overlap. Two Sparks → both might pick math-7B because GB10 is a math
beast. Coverage gaps elsewhere are fine because cloud picks up the slack.

- **When right**: small clusters (1–3 nodes), hardware-heavy use case
  (running 70B models, image gen, video gen).
- **When wrong**: 5+ nodes, where redundancy on math is wasted while
  multilingual / vision is missing.

### Strategy B — "full set across mesh"

The allocator enforces coverage: ensure ≥1 node hosts each of
`{math, code, multilingual, general, vision, tool_use}`. If two nodes
could host math, only the cheapest/best-fit hosts it; the other is told
to host the next-most-needed specialist.

- **When right**: medium clusters (3–6 nodes), most prompts handled locally.
- **When wrong**: 1-node setup (forced to host only 1 specialist, defeats
  the point), 10+-node setup (need redundancy and capacity per specialist,
  not coverage of new specialists).

### Strategy C — "tiered coverage" (default, hybrid)

Three coverage tiers, allocator fills them in order:

1. **Tier 1 — Essentials**: math, code, general. Every cluster must have
   at least one node per tier-1 specialist before doubling up. Routes the
   bulk of traffic.
2. **Tier 2 — Important**: multilingual, tool_use, summarization. Filled
   after tier 1 is covered.
3. **Tier 3 — Specialized**: vision, embeddings, image-gen, whisper.
   Filled only when capacity allows AND user traffic shows demand
   (allocator sees 5%+ of last-100-prompts in that domain).

After all tiers are covered, the allocator promotes nodes to **replicas
of overloaded specialists**. If `math` is at 80% util while `tool_use` is
at 5%, the next idle slot becomes math-replica.

This is the **default** for the v0 ship. Lets a 1-Spark setup feel
complete (3 tier-1 specialists loaded), a 2-Spark setup get tier 1 + 2,
and bigger meshes earn redundancy.

### Decision logic

```python
def allocate_cluster(
    nodes: list[NodeProbe],
    catalog: list[SpecialistCard],
    traffic_mix: dict[DomainId, float],  # last 24h domain shares, 0..1
    strategy: Literal["best_per_machine", "full_set", "tiered"] = "tiered",
) -> dict[NodeId, NodeSuggestion]:
    ...
```

## 5. Registry / heartbeat contract

Lives on slancha-api as a small FastAPI service. Event-sourced (mirroring
exo's pattern), so we get persistence + replay for free.

### Endpoints

```
POST /mesh/v1/heartbeat
  body: NodeHeartbeat (see below)
  rate: every 5s per node
  response: { ack, next_due_seconds, allocator_suggestion? }

GET  /mesh/v1/registry
  response: { nodes: [...], specialists: [...], coverage: {...} }
  used by: slancha-router on each request (cached 1s)

POST /mesh/v1/probe-network
  body: { from_node_id, to_node_id }
  triggers active bandwidth + RTT probe; result returned via next heartbeat

POST /mesh/v1/allocate
  body: { strategy, force? }
  re-runs the allocator; usually triggered internally on topology change
  but exposed for ops
```

### NodeHeartbeat

```python
@dataclass(frozen=True)
class NodeHeartbeat:
    node_id: NodeId
    ts: datetime
    hardware: NodeProbe                    # full re-send every 60s, lite delta otherwise
    loaded_models: list[LoadedModel]       # what's hot right now
    util: NodeUtilization                  # gpu%, ram%, queue_depth, p50/p95 latency last 60s
    recent_throughput: dict[ModelId, float]  # tok/s smoothed over last 60s per model
    health: Literal["healthy", "degraded", "draining", "training"]
    network_view: dict[NodeId, NetworkLink]  # observed RTT/bandwidth to other nodes
```

`health = "training"` means the node has voluntarily taken itself out of
the inference rotation to run an idle-fine-tune. Allocator excludes it
from primary routing during this state but keeps it as a fallback.

### Registry view used by router

```python
@dataclass(frozen=True)
class RegistrySnapshot:
    snapshot_ts: datetime
    nodes: dict[NodeId, NodeSummary]
    specialists: dict[SpecialistId, list[NodeBinding]]  # which nodes host each specialist
    coverage: dict[DomainId, list[NodeId]]
    # For router decision
    ranked_routes: dict[tuple[DomainId, DifficultyTier], list[Route]]
```

`Route = (specialist_id, node_id, estimated_queue_ms, p95_latency_ms,
cost_estimate_cents)`. The router takes the registry snapshot, intersects
with classifier signals, picks top route, falls back through the list on
failure.

## 6. Router extension (slancha-api side)

Extend the existing `select_model_lmarena()` (already has `prefer_pareto`
mode from prior session) to return `(model, node_url)` instead of `(model,)`.

```python
@dataclass(frozen=True)
class MeshSelectionResult(SelectionResult):
    node_id: NodeId | None         # None = cloud fallback
    node_url: str | None           # the OpenAI-compatible base URL of the chosen node
    queue_ms_estimated: int
    cluster_coverage_used: bool    # was this resolved by mesh or by cloud?
    fallback_chain: list[tuple[ModelId, NodeId | None]]
```

### Decision flow

1. Classifier runs on slancha-api (unchanged).
2. Router calls `registry.lookup(domain, difficulty, language)` → list of
   `Route` candidates ordered by `_pareto_score(spec, node)`.
3. Filter out nodes with `health != "healthy"` and nodes where
   `queue_ms_estimated > 2000` (configurable per-tenant).
4. If no mesh route survives → fall through to slancha-cloud catalog.
5. Forward request to chosen node's `node_url` with OpenAI-compatible body.
6. On 5xx / timeout / token-stream-truncation → retry next item in
   fallback chain.

### Per-route latency budget

Each route declares a budget; allocator honors it when balancing.

| Route class | Budget (TTFT + total) | Examples |
|---|---|---|
| Hot interactive | <500ms TTFT, <3s total | autocomplete, chat |
| Standard | <2s TTFT, <15s total | Q&A, code completion |
| Batch | unlimited | summarization, bulk |

Tier hot-interactive routes only use nodes where `p95_latency < 1500ms`
in last 60s, even if higher-scoring specialists exist elsewhere.

## 7. Idle fine-tune daemon

Runs on each node. Watches `util.gpu < 10%` AND `util.queue_depth == 0` for
60s straight. Then:

1. Sets `health = "training"` in next heartbeat — registry tells router to
   stop sending hot-interactive traffic; batch routes can still arrive,
   training pauses on each request and resumes after.
2. Pulls recent traffic from slancha-api (oracled with cloud responses or
   user feedback) for this node's primary specialist's domain.
3. Runs a LoRA pass (small, ~30min, checkpoint every 100 steps).
4. On preempt signal (incoming hot request when `health = "training"`),
   yields immediately: save LoRA state, return to `health = "healthy"`,
   resume training next idle window.
5. After N hours of cumulative training, merges the LoRA into a new
   `qwen-math-7b-q4@$user-v3` and registers it as a new specialist
   variant. Allocator can route to either upstream or personalized.

**SPARTA gossip variant (later)**: nodes hosting the same specialist
exchange 0.1% of LoRA params per training step (per day-12). Compounds
across the cluster — your math model is informed by other clusters'
math model improvements, at 1000× lower bandwidth than full DiLoCo sync.
This is Phase 2; we don't ship it in v0.

## 8. Networking + transport

Borrow exo's libp2p substrate at first, but **the v0 doesn't need libp2p**.
Heartbeats can be plain HTTP POST to slancha-api. We add libp2p only when
we want:

- **Auto-discovery** without manual `node_url` configuration (exo's mDNS +
  Tailscale + LAN multi-path detection).
- **Direct node-to-node** transfers (model weight streaming, LoRA
  parameter gossip).
- **Cluster-state replication** when slancha-api is down.

For v0: each node knows the slancha-api URL via config. Heartbeats are
HTTP. Routing happens at slancha-api. Single point of failure is
acceptable in v0; v1 adds the libp2p layer for resilience.

**Connection priority** (from exo's TODO #15, worth borrowing):
`TB5 > Ethernet > WiFi > Tailscale > Internet`. We already saw this work
in the 2-Spark probe — exo found three paths and picked the fastest.
When we add libp2p, we get this for free.

## 9. Backend abstraction

Each node runs **one of**:

- `vllm`: best throughput for CUDA. Default for Sparks, RTX boxes. OpenAI-compatible.
- `llamacpp`: best for CPU + GGUF + mixed hardware. Default for Mac mini
  with limited unified mem, Framework Desktop.
- `ollama`: easiest first-time setup, OpenAI-compatible. Default if a node
  was already running Ollama before joining the mesh.
- `slancha-cloud-shim`: for nodes whose hardware can't run anything
  locally (e.g., a Raspberry Pi acting as edge proxy).

The registry doesn't care which — only that the node exposes an
OpenAI-compatible HTTP endpoint and reports the same heartbeat schema.

Node startup script picks backend by probe heuristic:
- CUDA 9.0+ and ≥16GB VRAM → vllm
- Apple Silicon ≥ M1 with ≥16GB unified → llamacpp (Metal) or mlx
- Anything else with ≥8GB → llamacpp CPU
- Otherwise → don't host inference, register as a registry-only node

## 10. Observability

Reuse Langfuse (per START_HERE.md). Each route through the mesh emits a
trace:

```
slancha-router → classify → registry.lookup → route_choice
                                            → forward to node
                                            → token stream
                                            → final response
```

Each span carries: `classifier_signals`, `chosen_specialist`, `chosen_node`,
`fallback_chain_used`, `queue_ms`, `actual_tps`, `vs_cloud_baseline_cost`.
We feed this back into the dashboard at `evals.laulpogan.com` as a new
panel: **Mesh hit rate** (% of requests served by mesh, not cloud), **AIQ
delta** (mesh AIQ minus cloud AIQ on same traffic), **specialist
utilization heatmap**.

## 11. Security boundaries

- **Heartbeat auth**: pre-shared key per node (`SLANCHA_NODE_TOKEN`),
  sent as bearer header. Bound to `node_id`. Rotated by user from
  slancha-api admin UI.
- **Inference traffic**: between slancha-api and node = mTLS over
  Tailscale (already deployed per global CLAUDE.md). Between node and
  Hugging Face / model registry = stock TLS + HF token.
- **LoRA fine-tune data**: stays on the node. Slancha-api forwards the
  prompt **only** to the node serving the request — does NOT centralize
  user traffic for cross-node training. SPARTA-style gossip (Phase 2)
  will share **parameter deltas**, not raw prompts. This is the privacy
  pitch.
- **Trust remote code**: model cards default `trust_remote_code = false`.
  Following exo's pattern; explicit opt-in required.

## 12. Ship plan

### v0 (2 weeks, what we build now)

| Day | Deliverable | Owner |
|---|---|---|
| 1 | Hardware probe + `NodeProbe` JSON. Tested on both Sparks. | slancha-test |
| 1 | Pick 5 tier-1 specialists; download Q4 quants; per-specialist bench on Spark | slancha-test |
| 2 | `SpecialistCard` TOML schema + 5 cards committed to `resources/specialists/` | slancha-api |
| 3 | `model_fit_score` + `allocate_cluster` pure functions; unit tests | slancha-api |
| 4 | Registry FastAPI subapp on slancha-api: heartbeat ingestion + `RegistrySnapshot` builder | slancha-api |
| 5 | vLLM provisioning script for Sparks — `./bring-up-mesh-node.sh` | slancha-test |
| 5 | Extend `select_model_lmarena` → `MeshSelectionResult` with node routing | slancha-api |
| 6 | Slancha-local rewrite: probe → heartbeat → serve via vLLM. ~200 LOC. | slancha-local |
| 7 | End-to-end: classify → mesh lookup → vLLM on Spark → response → trace in Langfuse | both |
| 8 | Replay 70-prompt corpus against mesh; record AIQ vs cloud baseline | slancha-test |
| 9 | Dashboard: mesh hit rate, specialist util, AIQ delta panels | slancha-test |
| 10 | Failure tests: kill a node mid-request, kill backend, network partition | slancha-test |
| 11 | Smoke harness wired to nightly cron on Spark | slancha-test |
| 12-14 | Docs, demo video, internal review | both |

Idle-fine-tune daemon is **out of v0**. Single-node fine-tune script is
in for power users to invoke manually, but no automation.

### v1 (next 4 weeks)

- Idle fine-tune daemon with checkpoint-on-preempt
- libp2p discovery substrate (drops dependency on manual `node_url`
  config; gives multi-path routing per exo day-1)
- Model preloading (start downloading the next-most-needed specialist
  when disk has space)
- Public Tailscale tunnel pattern (per global CLAUDE.md
  `forge.laulpogan.com` reference) for users without home LAN

### v2 (later)

- SPARTA-style parameter gossip across replicas of the same specialist
- evML-style remote attestation for nodes accepting external training
  traffic (per exo day 10)
- Cross-cluster mesh: federate multiple personal clusters into a
  collective network with opt-in compute sharing

## 13. What we steal from exo, explicitly

| exo feature | We adopt | We skip |
|---|---|---|
| libp2p discovery | Phase 2 | v0 uses HTTP heartbeats |
| Event-sourced state | ✓ (registry on slancha-api) | — |
| Master election (bully) | Not needed — slancha-api IS the master | — |
| Pipeline-parallel sharding | — | Workload mismatch |
| Tensor-parallel sharding | — | Workload mismatch |
| Model-card TOML | ✓ (extended with `domain`, `difficulty_tiers`, `estimated_tps_at`) | — |
| OpenAI/Claude/Ollama API surface | ✓ (slancha-api already has it) | — |
| RDMA over Thunderbolt 5 | — | Not relevant for our workload (no inter-layer activations) |
| Topology cycle detection | — | We don't need cycles; specialists fit on one node |
| Placement preview API | ✓ (renamed `/mesh/v1/allocate`) | — |
| Continuous batching | ✓ (vLLM handles it, free) | — |
| DiLoCo decentralized training | Phase 2 | — |
| SPARTA sparse parameter averaging | Phase 2 | — |
| evML edge-verified ML | Phase 2 (when accepting external nodes) | — |
| EXO Gym simulator | — | Don't need it; our routing is testable via replay harness |

The critical insight from reading exo days 1–12: **their architecture
solves a different problem than ours**. They want to fit one giant model
across consumer Macs. We want to route 90% of requests to small
specialists locally and stop paying for cloud inference. The mesh
substrate is the same; the placement and routing are completely
different.

## 14. Open questions / decisions before we ship

1. **Per-user model variants** — when a user fine-tunes their math
   specialist, does it stay on their cluster only, or do we offer
   opt-in upload to a SlanchaHub registry of personalized models? Privacy
   answer vs network effect.
2. **Failure mode when ALL nodes down** — cloud fallback. But what about
   when slancha-cloud is also down? Spec says return 503; do we want a
   degraded mode (last-known-good response cache)?
3. **Backend opinionation** — do we pick vLLM as the v0 reference and
   document llama.cpp/Ollama as alternatives, or support all three at
   parity from day 1? Vote for opinionated.
4. **License posture** — exo moved to a more open license in 2025 (their
   day 7 announcement). What's ours? Apache-2.0 matches the substrate;
   anti-commercial-clause if we want a moat. Decision needed before any
   public release.
5. **Cluster naming** — "Slancha-Mesh" or something else? "Mesh" is
   already a loaded word (Istio, etc.). "Personal AI Cluster"?
   "SlanchaSwarm"? Marketing TBD.

## 15. References

- `~/Source/exo/docs/architecture.md` — exo's event-sourcing pattern
- `~/Source/exo/docs/api.md` — endpoint shapes we copy
- `~/Source/exo/src/exo/master/placement.py` — cycle filtering, memory
  filtering, sharding constraint checks (we don't need cycles; we do
  need memory filtering)
- `~/Source/exo/resources/inference_model_cards/*.toml` — model card
  TOML format we extend
- exolabs.net/blog days 1–12 — pipeline parallel, DiLoCo, SPARTA, evML,
  private search, personal AI, Windows 98 LLM, etc.
- `docs/EXO_SPARK_PROBE_2026_05_15.md` — what actually works on Sparks today
- `docs/ROUTER_IDEAS_REPLAY_2026_05_12.md` — pareto-mode router work that
  this builds on
- `START_HERE.md` — Slancha eval harness goals (Pareto position, oracle
  gap, AIQ — all directly applicable to mesh evaluation)
