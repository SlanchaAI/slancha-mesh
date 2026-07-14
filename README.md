# Slancha-Mesh

**Federate your local LLM nodes — Macs, GPU boxes, small homelab rigs —
into one OpenAI-compatible endpoint with hardware-aware routing across
specialists.**

You probably already run Ollama or vLLM on one box. Slancha-Mesh is the
layer on top: it discovers every node on your LAN or tailnet, learns what
each one is good at (code / reasoning / multilingual / small-and-fast),
and routes each prompt to the right one. No central server required (one
is optional), and no data leaves your hardware. Apache-2.0.

**Status:** the discovery, routing, and heartbeat substrate is stable and
well-tested (~1,000 unit tests plus live demos on GB10 hardware). The
specialist catalog ships one bring-up-validated card
(`qwen3-coder-30b-a3b-fp8`) alongside ten draft cards spanning Ollama and
vLLM; see [`docs/CATALOG_STATUS.md`](docs/CATALOG_STATUS.md) for per-card
validation status.

## How it works

Slancha-Mesh has three pieces:

- **Nodes** run your models on your hardware (through Ollama, vLLM,
  llama.cpp, or MLX) and expose both an OpenAI-compatible endpoint and a
  small `/models` self-description.
- **Discovery** builds a routing table by pulling each node's
  self-description — over your LAN (an explicit `--peer` list) or a
  Tailscale / Headscale tailnet.
- **The router** presents one OpenAI `/v1` endpoint. For each request it
  looks up the target specialist, picks the best reachable node — using
  live queue depth and measured p95 latency to break ties — and proxies
  the call, falling through to the next node on failure.

## Quickstart — one box, Ollama already installed

The block below installs the project, serves a model through your
existing Ollama, and routes a real prompt through the mesh.

```bash
# 1. Install. uv is fastest; the plain pip path works identically.
#    uv:  curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/SlanchaAi/slancha-mesh.git
cd slancha-mesh
uv venv && source .venv/bin/activate && uv pip install -e ".[dev]"
# pip-only alternative (no uv):
# python -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"

# 2. Pull a model your hardware can serve, through your existing Ollama:
ollama pull qwen2.5-coder:7b-instruct-q4_K_M

# 3. Start the node — adopts your running Ollama daemon, serves node-info on :8088.
slancha-mesh up --specialist qwen2.5-coder-7b-q4-ollama

# 4. In another terminal: a drop-in OpenAI /v1 endpoint over your mesh.
slancha-mesh router --peer 127.0.0.1 --port 8080

# 5. In a third terminal: ask a question — same shape as api.openai.com /v1.
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen2.5-coder-7b-q4-ollama",
    "messages": [{"role":"user","content":"reverse a string in python"}]
  }' | jq -r '.choices[0].message.content'
```

Expected output from step 5 — a short code answer:

```text
You can reverse a string in Python with a slice:

    s = "hello"
    print(s[::-1])   # 'olleh'
```

That response came back from `localhost:8080` — your local router
discovered the node and proxied the prompt to the model on your own
Ollama daemon. No cloud, no API key, one box.

### Let the router pick the model

Install the classifier extra and start the router with `--auto-route`;
then `model: "auto"` routes each prompt by classified domain and
difficulty (a built-in mmBERT-small + treelite classifier, ~ms per
prompt, fully local — the model weights ship in the wheel, so this
works air-gapped):

```bash
pip install "slancha-mesh[classifier]"
slancha-mesh router --peer 127.0.0.1 --port 8080 --auto-route

curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "auto", "messages": [{"role":"user","content":"reverse a string in python"}]}'
```

The `X-Slancha-Specialist` response header names the model it picked,
and the router log shows the classified signals per request. Coding
prompts land on your coder, hard general prompts on your strongest
generalist, easy ones on the smallest model that covers them.

To inspect the routing table instead of sending a prompt:

```bash
# Reads local node-info, skips Tailscale, shows the node reachable
# with the specialist bound to your Ollama daemon's URL.
slancha-mesh discover --peer 127.0.0.1
```

### Which backend runs on your hardware?

The planner recommends an engine per OS. The table below is what actually
serves today versus what's recommended; pick the model size your VRAM
fits.

| Hardware | Backend | Notes |
|---|---|---|
| Apple Silicon Mac (e.g. 16GB) | **MLX** or Ollama | Planner prefers MLX (native Metal via `mlx_lm`); set `mlx_repo` on the card. Ollama is the zero-config fallback. Good for 7B Q4. |
| Windows + NVIDIA (e.g. 8GB) | **Ollama** | Native CUDA. vLLM is Linux/WSL-only. Good for 7B Q4. |
| Linux + NVIDIA ≥24GB (3090/4090) | **vLLM** | Throughput; FP8 on Ada/Hopper+, else AWQ. Ollama also works. |
| Linux + NVIDIA <24GB | **Ollama** | GGUF fits. |
| GB10 / DGX Spark (aarch64 unified) | **Ollama** | No official vLLM sm_121 wheels yet. |
| CPU-only | **llama.cpp** or Ollama | Planner prefers llama.cpp (native `llama-server`); set `gguf_path` on the card. Ollama also works. |

Ollama is the universal zero-config backend; vLLM adds throughput on
Linux. MLX (Apple Silicon, `mlx_repo`) and llama.cpp (any box with a
GGUF, `gguf_path`) are native paths the planner recommends.

## Quickstart — two boxes on a LAN

No Tailscale required. On a trusted LAN, exposing a node's node-info
endpoint on all interfaces needs an explicit acknowledgement:
`SLANCHA_AUTH_REQUIRED=false` (the network is your trust boundary) or a
shared `SLANCHA_NODE_TOKEN`.

On **box A** (say a Mac mini, `192.168.1.10`):

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve              # one-time: bind Ollama on the LAN
ollama pull phi4-mini:3.8b-q4_K_M
SLANCHA_AUTH_REQUIRED=false \
  slancha-mesh up --specialist phi-4-mini-q4-ollama --node-info-host 0.0.0.0
```

On **box B** (say a 3090 box, `192.168.1.20`):

```bash
ollama pull qwen2.5-coder:7b-instruct-q4_K_M
SLANCHA_AUTH_REQUIRED=false \
  slancha-mesh up --specialist qwen2.5-coder-7b-q4-ollama --node-info-host 0.0.0.0
```

From either box (or your laptop), federate:

```bash
slancha-mesh discover --peer 192.168.1.10 --peer 192.168.1.20
```

You get a routing table that knows box A is good at small/easy prompts
and box B is good at code. The router takes a classifier verdict and
picks the right node from `domain` + `difficulty_tiers` + live queue
depth + measured p95 latency.

See [`docs/HOMELAB.md`](docs/HOMELAB.md) for the longer walkthrough
(2-GPU rigs, mixed Mac+Linux, fault-tolerant routing).

## Why not just…

| Existing tool | What it does well | What Slancha-Mesh adds |
|---|---|---|
| **Ollama / LM Studio** | Easy single-box model serving; great UX. | Federating *N* such boxes into one routed endpoint with hardware-aware specialist allocation. Ollama is a first-class backend here. |
| **exo / petals** | Splits *one* model's layers across nodes for memory-bound inference. | The opposite topology: route *different models* to *different nodes*. Complementary — use exo to run one 70B split across 4 Macs; use Slancha-Mesh to size each box for a specialist and route which specialist answers. |
| **vLLM / llama.cpp directly** | Best-in-class single-engine throughput. | The mesh treats them as backends behind one `/v1/chat/completions` seam; the engine choice happens behind that seam. |
| **LiteLLM / OpenRouter** | Unified API across N hosted providers. | The same OpenAI-compatible surface, but every node is yours on your hardware — no third-party billing, no data egress. |
| **llama-swap** | OpenAI-compatible proxy that hot-swaps which local model process runs on one box — good for VRAM-constrained model-juggling. | Cross-node discovery and federation: the router picks *which node* answers, not just which model is loaded on the one box. Complementary if you already run llama-swap on a node. |
| **SGLang** | High-performance serving engine (RadixAttention prefix caching, structured output, strong tool-call throughput). | It's an engine, not an orchestrator. Backends are a pluggable seam here; SGLang is a natural fit for that seam and is on the roadmap, not yet wired. |

## Components

| Module | What it does |
|---|---|
| `mesh/cli.py` | The `slancha-mesh` CLI: `up` / `discover` / `status` / `serve` / `doctor` / `plan`. |
| `mesh/discovery.py` | Pull discovery: walk a tailnet or an explicit `--peer` list into routes with host-pinned `node_url`s. |
| `mesh/node_server.py` | `build_node()` — the daemon and `/models` self-description share one registry. |
| `mesh/registry.py` | Event-sourced, thread-safe, deterministic replay. |
| `mesh/backends.py` | `VLLMBackend`, `OllamaBackend`, `LlamaCppBackend`, `MLXBackend`, `NullBackend`. The `BaseBackend` protocol is the seam — one class per engine. |
| `mesh/serve.py` | `ServeDaemon` boots backends and runs the heartbeat loop. |
| `mesh/select.py` | `select_mesh_route` — classifier verdict + snapshot → ranked routes with cloud fallback. |
| `mesh/allocator.py` | `model_fit_score` plus three cluster-allocation strategies. |
| `mesh/probe.py` | Hardware/network probe with GB10 unified-memory detection. |
| `mesh/catalog/*.toml` | 11 specialist cards (1 validated + 10 draft). |
| `mesh/tests/` | ~1,000 hermetic unit tests plus live-vLLM integration tests (gated by `VLLM_LIVE_URL`). |

## Backend support

| Backend | Status | How to use |
|---|---|---|
| `ollama` | Wired | Mac, AMD, Windows + NVIDIA, GB10, small NVIDIA. Adopts your running Ollama daemon at `127.0.0.1:11434` (or `OLLAMA_HOST=0.0.0.0:11434` for LAN). `OLLAMA_PORT` honored. |
| `vllm` | Wired (kernel-gated on Blackwell) | Linux/WSL + CUDA. Native FP8 on Hopper/Ada; Marlin weight-only fallback on Blackwell consumer (sm_120/sm_121). |
| `llamacpp` | Wired | Any box with a GGUF — the CPU-only / no-CUDA-no-Metal path. Owns (or adopts) a `llama-server` subprocess. Set `gguf_path` on the card (local path or `repo:file` HF id); needs `llama-server` on `PATH`. |
| `mlx` | Wired | Apple Silicon native (Metal). Owns an `mlx_lm.server` subprocess. Set `mlx_repo` on the card (an `mlx-community/...` HF repo); needs `mlx_lm` installed. Refuses on non-Darwin/arm64 hosts. |

## Multi-machine over Tailscale

Once you outgrow a single LAN — boxes on different networks, no port
forwarding, encrypted transport — Slancha-Mesh's pull discovery walks a
Tailscale / Headscale tailnet for `tag:specialist` peers and pulls each
node's `/models` over WireGuard. The tailnet ACL is the credential;
nothing is exposed to the open internet.

```bash
slancha-mesh up --tailnet --auto --key tskey-...
slancha-mesh discover --tailnet
```

See [`ONBOARDING.md`](ONBOARDING.md) for the full tailnet bring-up
(tagging, ACL shape, MagicDNS resolution, the `tag:specialist` membrane).

## Running and operating

```bash
# Unit tests (~3s)
uv run pytest mesh/tests/ -v

# Live vLLM integration tests (require a running vLLM)
VLLM_LIVE_URL=http://127.0.0.1:8001 \
  uv run pytest mesh/tests/test_integration_vllm.py -v

# Probe the local machine
uv run python -m mesh.probe --pretty

# What would the mesh allocate to this box?
slancha-mesh plan

# Diagnose tagged-but-undiscoverable nodes, ACL gaps, etc.
slancha-mesh doctor

# Run the node boot-persistent (systemd / launchd / Windows task) — see NODE_SETUP.md
slancha-mesh service install   # defaults to `up --auto`

# Bring up a Spark node end-to-end (probe → vLLM serve → smoke test).
# --trust-remote-code and HF-revision pinning are opt-in (supply-chain safe by default):
#   MESH_TRUST_REMOTE_CODE=1 MESH_MODEL_REVISION=<sha> bash mesh/scripts/bring-up-spark.sh ...
bash mesh/scripts/bring-up-spark.sh qwen3-coder-30b-a3b-fp8 8001
```

On GB10 (Blackwell sm_121), vLLM has no official FP8 GEMM kernel yet, so
the 30B-FP8 weights currently OOM. The validated bring-up served a
smaller cached model under the catalog's `--served-model-name` to
exercise the routing, discovery, and backend-lifecycle path — all of
which are model-agnostic.

## Two control planes share `:8088` — pick one

Discovery and the optional central registry both default to `:8088` but
mean opposite things:

- **Pull / per-node `/models`** (the default; what `slancha-mesh up`
  runs): each node serves its own self-description, and a consumer walks
  the tailnet or your `--peer` list and pulls. No central server, no
  shared write token.
- **Push / central registry** (optional, for ops dashboards or
  slancha-api integration): one shared `MeshRegistry` behind
  `POST /heartbeat` + `GET /registry`. Run it standalone with the
  [`docker/`](docker/docker-compose.yml) image, or mount
  `mesh.registry_app` into slancha-api
  ([Wire to slancha-api](#wire-to-slancha-api)).

There is exactly **one default per role**. Mixing them on the same path
produces a node that's alive but invisible (in nobody's routing table) or
one that shows up twice.

| | **Pull** (discover) — the local default | **Push** (registry) — the service tier |
|---|---|---|
| **Use when** | one box, a LAN, or a tailnet you can name | a multi-tenant gateway, ops dashboards, or slancha-api — anything aggregating nodes it doesn't control |
| **Who decides membership** | the consumer walks the network and finds live nodes | the node announces itself to a central registry and heartbeats |
| **Central server** | none | a `MeshRegistry` control plane |
| **A dead node** | silently drops out of discovery | stops heartbeating; the registry ages it out |

The only time both run at once is when a node lives on the local mesh and
also pushes to a separate hosted gateway for a different audience — and
even then, each consumer builds its routing table from exactly one plane,
so the node is counted once.

**Rule of thumb:** local / LAN / tailnet → pull. service / cloud / ops → push.

## Port convention

On a tailnet the ACL is deny-by-default, so ports are not interchangeable.
One rule the rest of the docs assume:

> If a node reports itself as discoverable, its advertised model URL must
> be reachable from the documented gateway under the documented ACL.

On a Tailscale / Headscale mesh:

- **`:8003` (vLLM) / `:8004` (HF)** — the model ports, and the only ones
  the gateway ACL opens (`tag:gateway -> tag:specialist:8003,8004`).
  `--base-port` defaults to `8003`, so you land here automatically.
- **`:8088`** — node-info / discovery only (the `/models` self-description
  the gateway pulls). Never a model port.
- **`:8000` (slancha-local) / `:8001` (vLLM dev)** — off-ACL. A node
  serving here registers fine but is unroutable: the gateway can't dial
  it. Re-serve on `:8003` (`slancha-mesh up --base-port 8003`).
  `slancha-mesh doctor` warns on exactly this.

LAN mode (`--peer`, no Tailscale) has no ACL membrane, so any reachable
port is fine; the invariant only binds where an ACL gates reachability.

## Design decisions

- **Bandwidth, not just VRAM, decides where the interactive hot path
  goes.** Measured (zero-install ctypes bench, MBU 0.82): an RTX PRO 6000
  Blackwell sustains ~1467 GB/s against GB10's 273 GB/s datasheet figure.
  Live on GB10, a small resident model decodes at 46 tok/s while a large
  one on the same box drops to 8 tok/s — a 30 tok/s interactive floor
  discriminates correctly between the two. Full derivation:
  [`docs/SIZING_BANDWIDTH_BRIEF.md`](docs/SIZING_BANDWIDTH_BRIEF.md).
- **Unified-memory nodes get `RAM − 8GB OS reserve`** as their effective
  model-fit budget. GB10 reports `[N/A]` for VRAM via nvidia-smi; the
  probe detects this and falls back to RAM with a warning.
- **The tiered allocator diversifies before duplicating.** A 2-Spark
  cluster gets one math and one code specialist; a 5-Spark cluster gets
  three tier-1 specialists plus two replicas of the highest-traffic domain.
- **Routes are pre-ranked at snapshot time**, not per request.
- **Snapshot replay is pure** from the event log, and the registry is
  thread-safe under concurrent `POST /heartbeat`.
- **The backend abstraction swaps engines without router changes.**
  `ServeDaemon` doesn't know vLLM or Ollama exists — only `BaseBackend`
  does.
- **Adopt, don't own, the local daemon.** `VLLMBackend` adopts a
  port-busy `vllm serve`; `OllamaBackend` adopts your running Ollama
  daemon. The mesh never SIGTERMs a process it didn't spawn.
- **Heartbeats report degraded, never crash the daemon.** A backend death
  becomes `health="degraded"` with `loaded_models=[]` on the next
  heartbeat, and the router falls through to the next route.

## Extending

### Add a specialist

Drop a TOML into `mesh/catalog/` matching the `SpecialistCard` schema. To
actually serve, set `required_backend` to a wired engine and provide the
matching field (`ollama_tag`, `model_id`, `mlx_repo`, or `gguf_path`).
Working examples: `mesh/catalog/qwen2.5-coder-7b-q4-ollama.toml` (Ollama)
and `mesh/catalog/qwen3-coder-30b-a3b-fp8.toml` (vLLM).

### Add a backend

1. Append to the `Backend` literal in `mesh/models.py`.
2. Add detection to `mesh/probe.py:_detect_backends`.
3. Implement the `BaseBackend` protocol in `mesh/backends.py` (mirror
   `OllamaBackend` for adopt-the-daemon, or `VLLMBackend` for
   own-the-subprocess).
4. Add a branch in `mesh/serve.py:build_backend()`.

### Wire to slancha-api

This is an optional central-registry (push) mode; the standalone mesh
above is pull-only and needs none of it. `mesh/registry.py` exposes the
FastAPI request/response shapes (`HeartbeatPostRequest`,
`RegistryGetResponse`). Mount
`mesh.registry_app.create_mesh_app(registry=shared_registry)` on
slancha-api at `/mesh/v1`.

### Plug into an existing selector

`mesh/select.py:select_mesh_route` returns a `MeshSelectionResult` that
extends slancha-api's `SelectionResult`. Call it before falling through to
`select_model_lmarena`; on `cluster_coverage_used=False`, defer to the
existing cloud selector.

## License

Apache-2.0. See [`LICENSE`](LICENSE).
