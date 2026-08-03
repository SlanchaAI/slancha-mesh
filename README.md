# Slancha-Mesh

**One private, OpenAI-compatible endpoint across the local model servers you
already run.** Slancha-Mesh discovers Ollama, vLLM, llama.cpp, and MLX
specialists across mixed hardware, routes a chosen model to a healthy node,
and returns a typed `punt` when local inference cannot serve the request.

With automatic policy enabled, the caller can target the weakest sufficient
specialist while Mesh handles live node readiness and failover. Cloud execution
stays an explicit, caller-owned decision: Mesh holds no provider credentials
and never turns a local failure into paid traffic. Optional adapters add image,
audio, transcription, and video routes without making media runtimes part of
the core.

Each node serves a whole model. Mesh chooses among model/node replicas; it does
not split one model across machines.

> **Project status:** `0.1.0a1` alpha. Install the exact source revision shown
> below; no PyPI package or GitHub release has been published yet. APIs and
> configuration may change, and the project has no service-level agreement.
> The core serving and routing package is separate from the optional tuning
> harness.

## First value: route one Ollama model through Mesh

This path needs no Docker, cloud account, or application programming interface
(API) key. It proves the Mesh hop with response headers rather than relying on
the generated text.

### Prerequisites

- macOS or Linux with Python 3.11–3.13, Git, and `curl`; Windows nodes are
  supported, but this canonical quickstart uses a POSIX shell
- [Ollama](https://ollama.com/download) installed and running
- about 9.3 GB for the model download; the catalog estimates 11 GB of runtime
  memory, so 12 GB of free system RAM, VRAM, or unified memory available to the
  model is the minimum and 16 GB leaves useful headroom
- three concurrent terminals after setup

Docker is needed only for the optional vLLM Semantic Router front door below.
The quickstart uses the catalog's live-validated `qwen3:14b` Ollama tag. The
download is real and large; run `slancha-mesh plan --json` first and choose a
smaller draft card only if you accept that its exact activation path is not yet
live-validated.

### Setup

```bash
git clone https://github.com/SlanchaAI/slancha-mesh.git
cd slancha-mesh
git checkout --detach 530a6e66aa3bd1a2649344a8cb5ef759afe6a0db
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
slancha-mesh plan --json
```

Inspect the plan and stop unless `ollama` appears in
`hardware.available_backends` and either `hardware.ram_available_gb` or
`hardware.vram_available_gb` is at least `12`. The planner may recommend a
smaller specialist; that is safer for constrained hardware, but draft cards do
not carry this quickstart's live-validation claim. Windows operators should use
[`NODE_SETUP.md`](NODE_SETUP.md) instead of translating this POSIX sequence.

Only after that check passes, download the validated model:

```bash
ollama pull qwen3:14b
```

### Terminal 1 — serve the specialist

Run from the repository with the virtual environment active. This adopts the
running Ollama daemon and exposes the node description on loopback `:8088`.

```bash
slancha-mesh up --specialist qwen3-14b-q4-ollama
```

Leave it running.

### Terminal 2 — run the Mesh router

```bash
cd slancha-mesh
source .venv/bin/activate
slancha-mesh router --peer 127.0.0.1 --port 8080
```

Leave it running. The router refreshes the node description and builds its
routing table.

### Terminal 3 — send a request

```bash
cd slancha-mesh
source .venv/bin/activate
curl -i http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen3-14b-q4-ollama",
    "messages": [{"role":"user","content":"reverse a string in python"}]
  }'
```

The answer varies, but a successful Mesh route has this observable shape:

```text
HTTP/1.1 200 OK
x-slancha-specialist: qwen3-14b-q4-ollama
x-slancha-node: <local-node-id>
x-slancha-reason: primary; queue_depth=... p95=...

{"choices":[...]}
```

`X-Slancha-Specialist`, `X-Slancha-Node`, and `X-Slancha-Reason` prove which
specialist and node served the request. Diagnose setup with:

```bash
slancha-mesh discover --peer 127.0.0.1
curl --fail http://127.0.0.1:8080/health
```

Treat catalog hardware fit as a preflight estimate and report successful
bring-up data.

### Second value: prove federation and failover

After the same specialist is healthy on two machines, confirm both names
resolve from the gateway and both node-info endpoints answer before replacing
the one-peer router. The commands below assume the ACL-gated tailnet setup
later in this README, where application-layer auth is deliberately disabled.
For the token-protected LAN setup, add
`-H "Authorization: Bearer $SLANCHA_NODE_TOKEN"` to every `curl` here and in
Terminal 3; `discover` and `router` read the same exported token themselves.

```bash
getent hosts node-a node-b                 # Linux; use `dscacheutil -q host -a name ...` on macOS
curl --fail http://node-a:8088/models
curl --fail http://node-b:8088/models
slancha-mesh discover --peer node-a --peer node-b
# In Terminal 2, stop the one-peer router with Ctrl-C, then replace it:
SLANCHA_ROUTER_BINDING_RETAIN_S=0 \
  slancha-mesh router --peer node-a --peer node-b --port 8080 --refresh-s 2
```

Send the Terminal 3 request and record `X-Slancha-Node`. Stop the foreground
`slancha-mesh up` process on that selected node, wait three seconds, and repeat
the request. Success means HTTP 200 returns with the same
`X-Slancha-Specialist` and a different `X-Slancha-Node`; restart the stopped
node afterward. The zero-retention setting makes this a deterministic failover
test; production defaults retain a missed binding for two refresh cycles to
smooth brief discovery loss. Stopping `up` removes the node description while
leaving an adopted Ollama daemon running. Use the token or tailnet setup below
before doing this across machines, including the bearer header above when the
token path is active.

This manual path proves discovery failover after a node disappears. The
separate request-time retry path handles connection failures and upstream
502/503/504 responses before any response bytes reach the caller; prove that
bounded behavior without faulting a live model:

```bash
python -m pip install 'pytest>=8,<10'
python -m pytest -q \
  mesh/tests/test_router_app.py::test_chat_completions_falls_through_on_connect_failure_to_next_binding \
  mesh/tests/test_router_app.py::test_chat_completions_retries_on_upstream_502_503_504
```

## What Mesh owns

```text
OpenAI-compatible client
          |
          v
  optional caller policy ---------> caller-approved cloud request
          |                              (outside Mesh)
          v
  Slancha-Mesh router :8080
          |
          +-- pulls /models from node-info :8088
          +-- ranks healthy bindings by readiness, queue, and latency
          +-- retries another eligible local node before a typed punt
          |
          +-- Ollama / vLLM / llama.cpp / MLX specialists
```

- **Nodes** run models on your hardware and expose an OpenAI-compatible model
  endpoint plus a small `/models` self-description.
- **Pull discovery** walks an explicit LAN peer list or a
  Tailscale/Headscale tailnet. A private mesh needs no central registry.
- **The Mesh router** host-pins discovered model URLs, filters unavailable
  bindings, and chooses among replicas of the requested specialist using live
  queue depth and measured p95 latency.
- **Model intent** comes from the request, the optional built-in classifier, or
  a caller-side policy layer. Mesh does not learn a routing policy at runtime.
- **Cloud policy** remains outside Mesh. A typed punt is data, not an automatic
  fallback.

This is the opposite of splitting one model across machines. Slancha-Mesh
places different specialists on different nodes and routes requests between
them.

## Automatic and semantic routing are optional

### Built-in offline classifier

Install the classifier extra to resolve `model: "auto"` without Docker. Its
assets are bundled for offline use; it maps prompt signals to catalog
specialists, then live Mesh readiness chooses the node.

```bash
python -m pip install -e ".[classifier]"
slancha-mesh router --peer 127.0.0.1 --port 8080 --auto-route
```

Leave that router running. From another activated terminal:

```bash
curl -i http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"write a Python parser"}]}'
```

Success is HTTP 200 with `X-Slancha-Specialist`, `X-Slancha-Node`, and
`X-Slancha-Reason` headers naming the resolved local route. Roll back by
stopping this router and restarting the explicit-model command from Terminal 2;
the bundled classifier assets do not run outside `--auto-route`.

### vLLM Semantic Router front door

[vLLM Semantic Router](https://github.com/vllm-project/semantic-router) is an
optional caller-facing layer, not a serving engine replacement and not a
first-run dependency. The `v0.3.0` multi-architecture image is pinned to OCI
digest `sha256:667c4d45e03fcee84d33792e6901fa3ac0e6a1f53e6a2674ecb1174e1decea64`.
The composition listens on `:8888` and forwards to Mesh on `:8080`:

```text
client :8888 -> vLLM Semantic Router -> Mesh :8080 -> local specialist
```

Start Mesh with `--auto-route`, then in another terminal with Docker running:

```bash
slancha-mesh semantic-router install \
  --config examples/oss-routing/vllm-semantic-router.yaml
slancha-mesh semantic-router validate
slancha-mesh semantic-router serve
```

Send `model: "MoM"` (model-of-models) to `http://127.0.0.1:8888`. The bundled
profile uses a **static** decision that maps MoM to Mesh `model: "auto"`; Mesh
then applies live fleet readiness. It does not enable learned semantic model
selection by default. Configure a vLLM Semantic Router decision with multiple
model candidates when you want that router to own semantic selection.

See [`examples/oss-routing/`](examples/oss-routing/) for the exact topology,
port-offset behavior, and rollback. Media calls bypass this alpha front door
and go directly to Mesh `:8080`.

## Local-first cloud escalation

When no eligible local binding can serve a request, Mesh returns HTTP 503 with
a stable machine-readable contract:

```http
HTTP/1.1 503 Service Unavailable
X-Slancha-Outcome: punt
X-Slancha-Reason: <bounded local diagnostic>
Content-Type: application/json

{"error":{"type":"slancha_punt","code":"local_route_unavailable",
"message":"Every attempted local route failed.",
"details":{"local_attempts":2,"suggested_class":"cloud","retryable":true}}}
```

The caller may queue, ask for consent, or make a separate request through an
external gateway. A 503 without `X-Slancha-Outcome: punt` is an ordinary
failure, not permission to spend.

A private Barkeep adapter in the reference deployment consumes this policy
boundary: it reads the typed punt, applies subscription/free/paid lane policy,
and requires an explicit spend decision before one cloud request. It is not a
public Mesh dependency or an installable OSS integration. Provider credentials
stay in the caller's credential boundary and never enter Mesh. Inference
Gateway, LiteLLM, or another caller-controlled executor can implement the same
external role.

## Optional multimodal routes

Core Mesh can proxy bounded, capability-advertised media protocols, but it
does not bundle LocalAI, vLLM-Omni, media models, CUDA, codecs, or a populated
media card in the built-in catalog. Operators run and pin an optional runtime,
copy an example card, and advertise only routes proven by that loaded model.

Supported direct-Mesh `:8080` routes in this alpha:

| Request shape | Routes |
|---|---|
| JSON | `POST /v1/images/generations`, `/v1/audio/speech`, `/v1/audio/generate`, LocalAI `/video` |
| Multipart | `POST /v1/images/edits`, `/v1/audio/transcriptions` |
| Async video jobs | `POST /v1/videos`, then `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content`, `DELETE /v1/videos/{id}` |

Async video jobs are owner-pinned: Mesh keeps a durable SQLite owner map so
poll, content, and delete calls return to the node and upstream origin that
created the job, including after a router restart. The alpha does not route
WebSocket video or vLLM-Omni `/v1/videos/sync`.

See [`examples/multimodal/`](examples/multimodal/) for pinned LocalAI and
vLLM-Omni integration seams, protocol maps, security boundaries, and curl
examples. The adapters have real-socket proxy tests against synthetic
runtime-shaped upstreams; they have not completed a live LocalAI or vLLM-Omni
model request. An operator-provided runtime and model remain required.

## Add machines safely

Start with a preflight on every candidate node:

```bash
slancha-mesh plan --json
```

The plan reports the detected backend, hardware fit, recommended specialist,
mesh state, and next steps. Catalog capacity and throughput values are
estimates until a live bring-up records them; choose a smaller model when the
runtime has insufficient memory or storage.

| Hardware | Preferred backend | Practical starting point |
|---|---|---|
| Apple Silicon | MLX or Ollama | 7B Q4 on a 16 GB machine |
| Windows + NVIDIA | Ollama | 7B Q4 on an 8 GB GPU |
| Linux + NVIDIA, 24 GB or more | vLLM | Throughput-oriented serving; choose a quantization supported by the GPU |
| Linux + NVIDIA, under 24 GB | Ollama | GGUF models sized to available VRAM |
| GB10 / unified memory | Ollama | Size from free unified memory; verify architecture-specific engine support |
| CPU-only | llama.cpp or Ollama | Small GGUF models; expect lower throughput |

The planner prefers native MLX on Apple Silicon, llama.cpp for a local GGUF on
CPU, Ollama for broad compatibility, and vLLM on supported Linux/CUDA hosts.
Run [`slancha-mesh plan --json`](NODE_SETUP.md) before installing a heavy
engine or downloading large weights.

### Trusted LAN

A LAN is a trust boundary, not authentication. `SLANCHA_NODE_TOKEN` protects
Mesh node-info and router endpoints; it does **not** protect Ollama's model
port. Never bind Ollama to `0.0.0.0` on a multihomed host. The example below is
Linux-only and requires a private node address plus an input firewall that
permits only the gateway. On macOS or Windows, use the tailnet path unless you
have an equivalent tested host-firewall rule.

On the node, replace both addresses, install the two temporary `iptables`
rules, verify their order, then bind Ollama only to the private address:

```bash
NODE_LAN_IP=192.168.50.10
GATEWAY_LAN_IP=192.168.50.5
sudo iptables -I INPUT 1 -p tcp -d "$NODE_LAN_IP" --dport 11434 ! -s "$GATEWAY_LAN_IP" -j REJECT
sudo iptables -I INPUT 1 -p tcp -s "$GATEWAY_LAN_IP" -d "$NODE_LAN_IP" --dport 11434 -j ACCEPT
sudo iptables -L INPUT --line-numbers -n | head
OLLAMA_HOST="$NODE_LAN_IP:11434" ollama serve
```

From a non-gateway LAN host, `curl --connect-timeout 2
http://192.168.50.10:11434/` must fail. From the gateway it must return HTTP
200. Do not continue until both checks behave that way. Leave Ollama running;
in another node terminal set the Mesh token:

```bash
# Silent input keeps the value out of shell history.
printf 'Node token: '
read -rs SLANCHA_NODE_TOKEN
printf '\n'
export SLANCHA_NODE_TOKEN
NODE_LAN_IP=192.168.50.10
OLLAMA_HOST="$NODE_LAN_IP:11434" slancha-mesh up \
  --specialist qwen3-14b-q4-ollama \
  --node-info-host 0.0.0.0
```

On the router box:

```bash
printf 'Node token: '
read -rs SLANCHA_NODE_TOKEN
printf '\n'
export SLANCHA_NODE_TOKEN
NODE_LAN_IP=192.168.50.10
slancha-mesh router --peer "$NODE_LAN_IP"
```

Enter the same high-entropy value from a secret manager at each prompt. Unset
it after the process inherits it; never paste the value into a command, config,
service argument, or tracked file.

`SLANCHA_AUTH_REQUIRED=false` disables the bind guard. Use it only on a
deliberately isolated, fully trusted network after accepting that node-info
and router endpoints have no application-layer authentication:

```bash
SLANCHA_AUTH_REQUIRED=false slancha-mesh up \
  --specialist <id> --node-info-host 0.0.0.0
```

For a multi-box walkthrough, see [`docs/HOMELAB.md`](docs/HOMELAB.md).

### Tailscale or Headscale

Tailnet mode uses membership plus restricted role tags as the credential. It
exposes model and node-info ports only to tagged gateways, not to the public
Internet. Before joining anything, adapt this HuJSON fragment in the Tailscale
policy editor; keep the tailnet's other rules and tests intact:

```jsonc
{
  "tagOwners": {
    "tag:specialist": ["autogroup:admin"],
    "tag:gateway": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["tag:gateway"],
      "proto": "tcp",
      "dst": ["tag:specialist:8003,8004,8088"]
    }
  ]
}
```

Tailscale recommends grants for new policies; this ACL form remains supported
and keeps the port boundary visible. Validate it in the policy editor before
continuing. See the official [policy syntax](https://tailscale.com/kb/1337/policy-syntax)
and [tag guide](https://tailscale.com/docs/features/tags).

Enroll the specialist interactively so no auth key appears in process
arguments or shell history:

```bash
sudo tailscale up --advertise-tags=tag:specialist
```

After browser login/admin approval, use two node terminals. Terminal A binds
Ollama only to the Tailscale address and the ACL-open model port:

```bash
TAILSCALE_IP=$(tailscale ip -4)
OLLAMA_HOST="$TAILSCALE_IP:8003" ollama serve
```

Terminal B exposes pull discovery on that same private interface. The explicit
auth opt-out is safe only because the policy above limits the listener to the
tagged gateway role:

```bash
TAILSCALE_IP=$(tailscale ip -4)
SLANCHA_AUTH_REQUIRED=false OLLAMA_HOST="$TAILSCALE_IP:8003" \
  slancha-mesh up --tailnet --specialist qwen3-14b-q4-ollama \
  --node-info-host "$TAILSCALE_IP"
```

Enroll a separate gateway interactively, then use one terminal for the router:

```bash
sudo tailscale up --advertise-tags=tag:gateway
slancha-mesh discover
slancha-mesh router --port 8080
```

From another gateway terminal, send the Terminal 3 request to
`http://127.0.0.1:8080`. Success requires HTTP 200 plus the expected
`X-Slancha-Specialist` and a remote `X-Slancha-Node` value.

Without `:8088`, discovery fails. Without the model port, a node can appear in
discovery but remain unroutable. Keep SSH and all other specialist ports denied
to `tag:gateway`. See [`ONBOARDING.md`](ONBOARDING.md) and
[`NODE_SETUP.md`](NODE_SETUP.md) for Headscale, tagged-key automation, MagicDNS,
and durable services.

## Backends

| Backend | Status | Boundary |
|---|---|---|
| Ollama | Wired | Adopts an existing daemon; supports macOS, Linux, Windows, NVIDIA, and unified-memory hosts. |
| vLLM | Wired | Linux/WSL + supported CUDA hardware; engine and quantization compatibility remains operator-owned. |
| llama.cpp | Wired | Owns or adopts `llama-server`; the card supplies a local or Hugging Face GGUF path. |
| MLX | Wired | Apple Silicon only; owns `mlx_lm.server`; the card supplies an MLX repository. |

Mesh does not stop a model daemon it did not start. vLLM, llama.cpp, and MLX
processes that Mesh spawns remain Mesh-owned; a process found on an already
busy port is health-tracked but detached on shutdown. Backend health degrades a
binding instead of crashing the router, and requests can fall through to the
next eligible replica.

## Operations and rollback

Install node, router, or semantic-router roles as boot-persistent services on
supported platforms after a foreground request succeeds. Mesh adopts Ollama,
so persist the private Ollama listener separately using the platform-specific
steps in [`NODE_SETUP.md`](NODE_SETUP.md#running-it-as-a-service-survives-reboots):

```bash
# macOS/Linux tailnet node; substitute the stable device address if needed.
TAILSCALE_IP=$(tailscale ip -4)
slancha-mesh service install --kind node \
  --env SLANCHA_AUTH_REQUIRED=false \
  --env OLLAMA_HOST="$TAILSCALE_IP:8003" \
  -- --tailnet --specialist qwen3-14b-q4-ollama \
  --node-info-host "$TAILSCALE_IP"
slancha-mesh service install --kind router -- --peer node.example.ts.net
slancha-mesh service install --kind semantic-router
slancha-mesh service status
```

Remove each managed role explicitly:

```bash
slancha-mesh service uninstall --kind semantic-router
slancha-mesh service uninstall --kind router
slancha-mesh service uninstall --kind node
```

For foreground setup, press Ctrl-C in the router and node terminals; adopted
model daemons remain running. To roll back the semantic front door, first run
`slancha-mesh semantic-router stop` and point clients back to
`http://127.0.0.1:8080`. If no asynchronous video jobs remain, archive router
state instead of deleting it:

```bash
mv ~/.local/state/slancha-mesh/router \
  ~/.local/state/slancha-mesh/router.rollback.$(date +%Y%m%d%H%M%S)
```

PowerShell equivalent:

```powershell
$state = Join-Path $HOME '.local\state\slancha-mesh\router'
Rename-Item $state ("router.rollback." + (Get-Date -Format yyyyMMddHHmmss))
```

For the Linux LAN example, first press Ctrl-C in the dedicated Ollama terminal
and confirm the gateway can no longer connect to `192.168.50.10:11434`. Only
then remove the two firewall rules in reverse order:

```bash
NODE_LAN_IP=192.168.50.10
GATEWAY_LAN_IP=192.168.50.5
sudo iptables -D INPUT -p tcp -s "$GATEWAY_LAN_IP" -d "$NODE_LAN_IP" --dport 11434 -j ACCEPT
sudo iptables -D INPUT -p tcp -d "$NODE_LAN_IP" --dport 11434 ! -s "$GATEWAY_LAN_IP" -j REJECT
! sudo iptables -C INPUT -p tcp -s "$GATEWAY_LAN_IP" -d "$NODE_LAN_IP" --dport 11434 -j ACCEPT
! sudo iptables -C INPUT -p tcp -d "$NODE_LAN_IP" --dport 11434 ! -s "$GATEWAY_LAN_IP" -j REJECT
```

For tailnet rollback, first press Ctrl-C in the dedicated Ollama Terminal A and
confirm a gateway can no longer connect to `<specialist-magicdns>:8003`. Then
remove the device's specialist/gateway tag in the tailnet admin console. Run
`tailscale logout` only when the whole device was enrolled solely for Mesh.
`ollama rm qwen3:14b` is optional and deletes the downloaded model; normal
rollback leaves it in the Ollama cache.

The canonical source install lives entirely in its clone and `.venv`. After
stopping every role above, archive both recoverably from the clone's parent:

```bash
deactivate 2>/dev/null || true
cd ..
mv slancha-mesh "slancha-mesh.rollback.$(date +%Y%m%d%H%M%S)"
```

Before enabling identity or peer-verification flags across a mixed-version
fleet, follow the ordered migration and rollback steps in
[`docs/UPGRADE.md`](docs/UPGRADE.md). The node setup guide covers systemd,
launchd, Windows Scheduled Tasks, verification, and troubleshooting:
[`NODE_SETUP.md`](NODE_SETUP.md).

Optional usage telemetry is off by default. Setting
`SLANCHA_USAGE_SINK_URL` emits routing metadata and token counts—not prompt or
completion bodies—to a receiver you run through a local, retrying JSONL spool.
If the request supplies an OpenAI-compatible `user` identifier, telemetry also
records and forwards that value. Mesh does not price events.

## Optional tuning add-on

Fine-tuning, replay evaluation, promotion gates, corpus tooling, and the
dashboard ship in `slancha-mesh-tune`. The core package neither imports nor
starts them.

```bash
python -m pip install -e ./packages/slancha-mesh-tune
slancha-mesh-tune check

# Only on a machine intended to train:
python -m pip install -e "./packages/slancha-mesh-tune[train]"
```

See [`packages/slancha-mesh-tune/README.md`](packages/slancha-mesh-tune/README.md).

## Why this layer exists

| Tool | Its strength | Mesh's separate job |
|---|---|---|
| Ollama / LM Studio | Easy single-box serving | Discover and route specialists across boxes. Ollama is a first-class backend. |
| vLLM / llama.cpp / SGLang | High-performance serving engines | Keep engine choice behind one fleet endpoint. SGLang is not wired yet. |
| exo / Petals | Split one model across nodes | Route different models to different nodes. The approaches are complementary. |
| vLLM Semantic Router | Caller-side semantic policy | Supply a changing, request-ready private fleet behind that policy. |
| Inference Gateway / LiteLLM / OpenRouter | Unified hosted-provider access | Return a typed local outcome; let the caller authorize provider spend and egress. |
| llama-swap | Swap local model processes on one constrained host | Choose which host and specialist receives the request. |

The narrow boundary is intentional: serving engines serve, semantic policy
expresses intent, Mesh federates private hardware, and the caller owns cloud
cost and credentials.

## Extend and contribute

- Add a specialist by copying a TOML card in [`mesh/catalog/`](mesh/catalog/)
  and advertising only a wired backend and verified capabilities.
- Add an engine by implementing the `BaseBackend` protocol and wiring backend
  detection and construction.
- Use the optional push registry only when a service or operations dashboard
  needs a central view. Home, LAN, and tailnet meshes default to pull discovery.

Development setup and checks live in [`CONTRIBUTING.md`](CONTRIBUTING.md).
Small, test-backed pull requests are welcome. Use GitHub Issues for reproducible
bugs and feature proposals; support is best-effort and carries no SLA.

## Security and license

Slancha-Mesh is a trusted-LAN or private-tailnet component, not a hardened
public-Internet gateway. Keep router, node-info, model, and media-runtime ports
behind that boundary. Report vulnerabilities through GitHub private
vulnerability reporting as described in [`SECURITY.md`](SECURITY.md).

Release-facing changes are recorded in [`CHANGELOG.md`](CHANGELOG.md).
Slancha-Mesh is licensed under Apache-2.0; see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).
