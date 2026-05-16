# exo on two-Spark cluster — actual situation

> Session: 2026-05-15. Goal: see what the exo-style local router situation
> actually looks like on the two DGX Sparks we have. Spawned from a James
> texts proposal to build a "compute swarm" router across Sparks + Mac mini
> + RTX cards.

## TL;DR

The exo mesh / topology / discovery / placement / OpenAI-compatible API
**all run fine on two NVIDIA DGX Sparks** with `mlx-none` install.
The **inference runner is MLX-only** and crashes with
`ModuleNotFoundError: No module named 'mlx'` the moment you try to actually
serve a model on Spark. NVIDIA Spark is on exo's "Planned Tier 1" list but
not implemented.

So today on Sparks, exo gives you: ✅ orchestration + topology + routing
surface ❌ no inference. For full inference today on Sparks you'd run
vLLM, llama.cpp RPC, or Petals behind exo's API or a custom router. Exo
once it ships its planned CUDA backend would slot in clean.

## What I did

1. Cloned exo v0.3.70 from `github.com/exo-explore/exo` onto
   `promaxgb10-d325` (Spark 1) and `spark-472e` (Spark 2). Both
   GB10 / aarch64 / Linux 6.17 / CUDA 13 / 128GB unified RAM.
2. Installed with `uv sync --no-dev --extra mlx-none` (mlx-none extra
   skips the MLX-Metal binary deps). Built the Svelte dashboard (which
   the Python module loader requires at import time even when serving
   no UI).
3. Started exo on Spark 1 with `--libp2p-port 4001 --offline
   --no-downloads`.
4. Pulled the libp2p multiaddr from the listening port + node id.
5. Started exo on Spark 2 with `--bootstrap-peers /ip4/100.91.57.17/tcp/
   4001/p2p/<peer-id>`.
6. Polled `/state` and `/topology` to confirm two-node discovery.
7. Asked for a model placement preview, committed it via
   `POST /place_instance`, observed what the runner did.
8. Inspected runner state to see the failure mode.
9. Cleaned up.

## What works on Sparks today

| Capability | Status | Notes |
|---|---|---|
| Install from source via `uv sync --extra mlx-none` | ✅ | 79 packages, ~2 min |
| Dashboard build (Svelte/Vite) | ✅ | `npm install && npm run build` in `dashboard/`. Required for import even if you don't browse to it. |
| CLI `exo` boots, master election, libp2p listener | ✅ | Default API on `:52415`, libp2p TCP/`:4001` when pinned |
| Two-node discovery via bootstrap-peers | ✅ | One-shot bootstrap, no manual config after |
| Multi-path topology detection | ✅ | Found 3 paths between Sparks: direct Ethernet (169.254.x.x), home LAN (192.168.x.x), Tailscale (100.x.x.x). Picks fastest at runtime. |
| OpenAI / Anthropic / Ollama API surface | ✅ | `/v1/chat/completions`, `/v1/messages`, `/ollama/*`, `/v1/responses` all routed |
| Memory + interface telemetry per node | ✅ | RAM total/available, swap, network interfaces |
| Placement planner | ✅ | Pipeline-sharding plan generated; assigns `worldSize=1` when model fits one node, multi-rank when not |
| **Inference runner** | ❌ | `ModuleNotFoundError: No module named 'mlx'` — runner imports `mlx.core` unconditionally |
| GPU detection | ❌ | Backend reports `MlxCpu` on both Sparks. No CUDA path. |
| RDMA / Thunderbolt | ❌ | "Linux Vulkan" + "Linux CUDA" are planned. `nodeThunderbolt` stays empty. |

Cluster state captured right after both Sparks joined:

```
topology.nodes = [
  "12D3KooWKAnma3XzunGiidKJMByqxgtNS4KUu2Gk4C5a9kyyx7ym",  # promaxgb10-d325
  "12D3KooWGjd8mU5Xmt3HGLVAKJVqyM99BCZafUi3N2GXn9mtssZS",  # spark-472e
]
topology.connections has bidirectional sinks across THREE paths each way:
  169.254.x.x  (direct Ethernet, link-local)
  192.168.x.x  (home LAN)
  100.x.x.x    (Tailscale)
```

Both nodes correctly reported ~130GB unified memory each, ~261GB combined
pool. The 169.254.x.x path means Spark-Spark traffic CAN go direct over
their Ethernet ports without bouncing through the home WiFi router. Good
for any future RDMA work.

## What breaks (and why)

When you ask exo to place a model, it generates a placement plan, spawns
a runner subprocess (`exo.worker.runner.bootstrap`), and the runner tries
to `import mlx.core as mx` on line 4 of
`src/exo/worker/engines/mlx/patches/opt_batch_gen.py`. MLX has no
CUDA / Linux ARM64 build. Runner dies immediately, the placement state
records `RunnerFailed`, and the API returns 404 on chat completions
because no instance is loaded.

```
RunnerFailed.errorMessage:
  "Terminated (exitcode=1
   Runner error: ModuleNotFoundError: No module named 'mlx'
   Traceback ... in exo.worker.engines.mlx.patches.opt_batch_gen.py
   import mlx.core as mx
   ModuleNotFoundError: No module named 'mlx'"
```

This is exo's stated plan: `PLATFORMS.md` lists "Linux CUDA Support —
Nvidia DGX Spark" under **Planned Tier 1**, not Tier 1. They have not
shipped the CUDA inference backend yet.

## Practical options for Slancha

### Option A — exo as routing/discovery layer only, plug in real inference backends behind it

Run exo for the topology + API + dashboard. Replace the MLX runner with
a thin shim that proxies to:
- **vLLM** on each Spark for production CUDA inference (works today,
  proven, has tensor-parallel and pipeline-parallel)
- **llama.cpp + `rpc-server`** for lighter quants
- **Slancha-cloud** as the fallback for prompts that don't have a local
  model loaded

Effort: ~1 week to write an `exo.worker.engines.openai_passthrough` adapter
that satisfies the runner interface but delegates inference. Slancha
would then use exo as the cluster fabric, not the inference engine.

### Option B — wait for exo's CUDA backend

It's on the roadmap. If exo Labs ships before our nightly schedule
needs it, the whole stack drops in. Risk: no public timeline, and their
roadmap also lists Linux CPU and Windows CUDA ahead of finishing
NVIDIA-Spark-specific work.

### Option C — build our own from the exo blueprint

Their architecture is the right shape: libp2p discovery, master election,
placement planner, multi-API surface, topology-aware sharding. The
**routing** part (which Slancha cares about more than tensor-parallel
sharding) is a small subset of what exo does. We could build a
Slancha-Mesh in ~2 weeks using the patterns we just saw:
- libp2p discovery → use go-libp2p or rust-libp2p; nodes advertise
  `(models_loaded, gpu_util, queue_depth, throughput_tps)`
- Master/coordinator → already exists in slancha-api; reuse the
  classifier-driven selector and extend pick to `(model, node)`
- Backend abstraction → each node runs vLLM/llama.cpp, exposes
  OpenAI-compatible HTTP, slancha-router selects which one based on
  current state

This is the path I'd take. Exo proves the architecture works on
heterogeneous hardware over multi-path links (we just saw it!), but
their value-add is tensor-parallel inference over Thunderbolt-RDMA, which
isn't Slancha's bottleneck. Our bottleneck is intelligent (classifier-
driven) per-prompt routing across a heterogeneous local pool. Exo
doesn't solve that — it would still need slancha-api's classifier on top.

### Option D — combine

Use exo for what works (topology/discovery/multi-path) and slancha-api
for what's missing (classifier routing + actual inference backends).
Exo becomes our "service mesh" layer; slancha-api becomes the
intelligence layer; vLLM/llama.cpp become the actual engines per node.
Three independent components, each doing what it's good at.

## Concrete next move (recommended)

Two-week spike:
1. **Day 1-2:** Get one vLLM instance serving a 7B model on Spark 1.
   Benchmark tokens/sec, time-to-first-token, memory profile. CUDA 13
   + GB10 is bleeding-edge — confirm vLLM actually loads.
2. **Day 3-4:** Same on Spark 2. Confirm both can serve simultaneously.
3. **Day 5-7:** Wire slancha-api as the front-door. Extend
   `select_model_lmarena` to return `(model, node_url)` and round-robin
   identical local replicas. Pipe trace through Langfuse.
4. **Day 8-10:** Build the gossip side. Each Spark posts heartbeats
   to a tiny `/registry` endpoint on slancha-api with current
   utilization. Selector uses the snapshot when picking node.
5. **Day 11-12:** Idle detector + LoRA fine-tune daemon. Sparks at
   <10% util for 60s claim themselves for offline training. Yield on
   inference-traffic signal.
6. **Day 13-14:** Bench against baseline: latency, throughput, cost.
   Write up.

This skips exo entirely. The exo probe today told us all we needed: the
mesh-layer ideas (gossip, multi-path, topology) are sound and tractable;
exo's MLX dependence makes it the wrong tool for us. The interesting
work for Slancha is the **routing intelligence**, which slancha-api
already has — we just need to extend it from `pick a model` to `pick a
(model, node)`.

## Artifacts left behind

- `/tmp/exo-spark1.log` — full master log including runner failure traceback
- `~/Source/exo/` — clone with mlx-none install; resumable
- spark-472e: `~/Source/exo/` cloned and installed there too
- Two-Spark cluster was running at session end; processes can be killed
  with `pkill -f 'exo --'` on each host

## Reproduce

```bash
# Spark 1 (master)
cd ~/Source/exo
uv sync --no-dev --extra mlx-none
(cd dashboard && npm install && npm run build)
uv run exo --no-downloads --offline --libp2p-port 4001 &

# Get the multiaddr (random suffix on each restart)
NODE_ID=$(curl -s http://localhost:52415/node_id | tr -d '"')
echo "/ip4/100.91.57.17/tcp/4001/p2p/$NODE_ID"

# Spark 2 (worker)
ssh admin@spark-472e
cd ~/Source/exo
uv sync --no-dev --extra mlx-none
uv run exo --offline --libp2p-port 4001 \
  --bootstrap-peers '/ip4/100.91.57.17/tcp/4001/p2p/<paste-master-id>'

# From either host
curl http://localhost:52415/state | jq '.topology'   # → 2 nodes, 3 paths
curl http://localhost:52415/v1/chat/completions \
  -d '{"model":"<any>","messages":[...]}'             # → 404 "no instance"
```
