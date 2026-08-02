# Session log — 2026-08-02

This public log preserves decisions and bounded verification evidence. Private
device names, addresses, user-linked labels, fleet size, and model-to-machine
placements are intentionally omitted.

## Private-tailnet deployment

Goal: make the OSS routing stack durable and reachable over a private tailnet,
using existing model servers without downloading weights or taking over their
lifecycle.

### Facts and decision

- The gateway already ran Mesh discovery and a supervised vLLM Semantic Router
  front door.
- Existing specialist services covered general, vision, and hard lanes. Mesh
  adopted them through external-backend cards instead of starting duplicate
  model processes.
- Model servers remain loopback-bound or behind tailnet-only forwarding. The
  gateway and specialists keep their existing lifecycle owners.
- Tailscale Serve publishes the gateway front doors privately. Funnel remains
  disabled.

### Durability and routing correction

Specialist node services run under enabled user-systemd units with lingering;
gateway router services run under the platform service manager. The semantic
router's container runtime starts at user login, so this is login persistence,
not a pre-login machine service.

Discovery initially produced a stable tie between general routes. The adopted
cards now claim distinct easy, medium/vision, and hard lanes. A vision server
that binds loopback uses a durable inner hop plus the existing tailnet-only
forward on convention port `:8003`; discovery host-pinning therefore reaches
the intended service without widening its bind address.

The generated router service replaced an older hand-written unit whose Python
environment no longer contained classifier dependencies.

### Live gate

From a second tailnet node, requests traversed private Tailscale TCP, vLLM
Semantic Router `:8888`, Mesh, and each configured difficulty lane. Easy,
medium, and hard requests all returned HTTP 200 with specialist and node audit
headers. Header values are omitted from this public log.

Router health reported every configured specialist reachable and routable,
closed runtime circuits, successful requests, and no failures or punts during
the gate. Serve retained the existing private `:8772` listener and added only
`:8888 -> 127.0.0.1:8888` for chat.

Live policy retrieval confirmed role-scoped access from gateway clients to
specialists on `8003`, `8011`, `8088`, and `11434`, including deny-by-default
self-tests. Tag ownership remains admin-restricted. No policy edit was needed.

HTTPS certificate automation still depends on a tailnet-wide admin toggle.
The current raw TCP proxy remains WireGuard-encrypted, policy-filtered, and
tailnet-only. Cloud punt consumption is not hidden in Mesh; local exhaustion
returns a typed punt for a caller-owned executor policy.

## Multimodal adapters and private media front door

Goal: expose trusted image/audio/edit/transcription/video routes without adding
a heavyweight runtime to Mesh core or weakening the chat path.

### Runtime examples

- Added pinned LocalAI 4.7.1 container config, exact protocol map,
  conservative external-card template, and image generation/edit, text to
  speech, transcription, and inline-video calls.
- Added pinned vLLM-Omni 0.20.0 startup guidance, exact protocol map,
  conservative external-card template, and image generation/edit, audio, and
  durable asynchronous-video calls.
- Each card advertises one conservative protocol. Operators must replace model
  identity, immutable revision where applicable, license, measured capacity,
  and add a capability only after proving that endpoint on the configured
  model.
- Neither runtime, CUDA, media codecs, nor weights became a core dependency.
  Primary sources are linked from `examples/multimodal/README.md`.

No LocalAI or vLLM-Omni service was present on the reachable SSH-capable
tailnet nodes. No runtime was installed and no weights were downloaded.
Status: adapters built, not wired; missing producer is an operator-configured
optional runtime/model service.

### Real request and socket evidence

A tiny inline-PNG request used the OpenAI vision-chat shape through the live
gateway. The request body and model output were discarded. It returned HTTP
200 and audit headers selected the explicit vision specialist on a healthy
tailnet route. Private header values and the data URI are omitted.

Real-TCP tests place runtime-shaped upstream and router applications on
loopback sockets. They inspect trusted method/path routing, JSON and multipart
content types, binary audio, upstream-only authorization, model aliasing, and
file bytes. The asynchronous video test creates a job, restarts the router with
the non-owner ranked first, then polls, downloads, and deletes through the
persisted owner. The decoy receives no calls.

### Tailscale Serve change

Before mutation, private TCP Serve contained `:8772` and chat `:8888`. The
additive command was:

```bash
tailscale serve --bg --tcp=8080 tcp://127.0.0.1:8080
```

After mutation, Serve contains `:8080 -> 127.0.0.1:8080`, `:8772`, and
`:8888`; each listener reports `tailnet only`. Funnel was not enabled and Mesh
still binds loopback. Rollback removes only the new listener:

```bash
tailscale serve --tcp=8080 off
```

A second tailnet node resolved the gateway through its local Tailscale netmap
without recording an address. `/health` returned HTTP 200 and reported all
configured routes ready; `/v1/models` returned HTTP 200 with the expected
vision route present.

Status: direct media front door wired and live. Chat remains on vLLM Semantic
Router `:8888`. Paid/cloud handoff remains caller-owned.

### Verification

- Runtime example/real-socket focus: 5 passed; Ruff clean; Docker Compose and
  both JSON protocol maps parsed.
- Router/media/video regression packet: 137 passed.
- Strict catalog validation: 18 cards clean.
- Full suite: 1,223 passed, 16 skipped, six pre-existing training-stub
  warnings.
- Whole core/add-on Ruff gate: clean.
- Release artifacts: two wheels plus two source distributions built; archive
  inspection and `twine check` passed all four; isolated installs proved core
  excludes training and the add-on composes at version `0.1.0a1`.

### Artifacts

- `examples/multimodal/README.md` — optional runtime boundary, startup, and
  adopted endpoint calls.
- `examples/multimodal/localai/` — pinned container, protocol map, card
  template.
- `examples/multimodal/vllm-omni/` — pinned runtime map and card template.
- `mesh/tests/test_multimodal_examples.py` — exact mapping/card drift gate.
- `mesh/tests/test_router_live_socket.py` — runtime-shaped wire and durable
  owner-pin proof.
- `examples/oss-routing/README.md` — split between chat `:8888`, media `:8080`,
  and caller-owned cloud policy.
