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

## Publication security gate

Fresh-eyes review found three release blockers and one durability major. The
router CLI accepted a discovery token flag that did not protect its listener;
video requests forwarded runtime-specific remote-reference and model-path
fields; committed prose contained private fleet identifiers; and video owner
expiry could discard the route needed to delete an upstream job.

The fixes keep listener authentication tied to `SLANCHA_NODE_TOKEN`, reject
unsafe video scalar fields before dispatch, replace private deployment details
with role labels, and retain expired owners in a leased cleanup outbox until
the exact upstream deletion succeeds. Failed owner persistence now attempts an
immediate compensating delete and durably queues retry work when the upstream
cannot be reached. Invalid protocol rows are quarantined without starving valid
cleanup work. Exact-owner uniqueness, quarantine revival, expiry merge, lease
recovery, restart recovery, and cross-store claim exclusivity have regression
tests.

Supply-chain examples now pin the LocalAI container by version and digest.
GitHub Actions use immutable commit hashes with readable version comments.
Node setup no longer presents authentication-disabled LAN serving as a normal
command.

Final local evidence after these fixes:

- Full suite: 1,255 passed, 16 skipped; six documented training-stub warnings.
- Focused router/media/security packet: 114 passed.
- Ruff: core and optional tuning package clean.
- Strict catalog validation: 18 cards clean.
- Release artifacts: core and tuning wheels/source distributions built,
  inspected, and accepted by `twine check` at version `0.1.0a1`.

The optional LocalAI and vLLM-Omni producers remain intentionally unwired on
the fleet: no compatible runtime/model was present, and installing a heavy
engine or downloading weights remains an operator decision.

A clean-room evidence pass then found the documented tailnet activation path
could not work: node-info stayed on loopback and the gateway commands followed
a foreground node command. It also found lifecycle, failover, diagnostics,
secret-handling, telemetry, and rollback claims that were incomplete or wrong.
The publication README now separates interactive tailnet enrollment from node
serving, binds Ollama and node-info to the Tailscale address, supplies an exact
tag/port policy and a routed request, and removes auth keys from process
arguments. The canonical model is the already live-validated `qwen3:14b` card.
Failover uses an explicit zero-retention test mode; local diagnostics no longer
invoke tailnet-scoped doctor checks.

Runtime lifecycle now matches the public contract: vLLM, llama.cpp, and MLX
detach from a process found on a busy port and stop only a child Mesh spawned.
The vLLM Semantic Router image is pinned by its multi-architecture OCI digest.
LAN guidance binds Ollama to one private address behind an explicit gateway-only
firewall rule, and rollback names every managed role plus state, firewall,
tailnet, and model-cache boundaries. Telemetry now discloses the optional
caller-supplied `user` identifier. The private Barkeep consumer and synthetic
multimodal-runtime evidence are labeled as such.

A final evidence pass caught one activation mismatch: the Ollama daemon bound
to a private address, while Mesh still derived its probe origin from the
generic node bind. `build_backend` now validates and honors the standard
`OLLAMA_HOST` origin, the LAN and tailnet commands pass the same origin to both
processes, and LAN rollback stops Ollama before removing its firewall rules.
Two regression tests cover private-interface selection and malformed origins.
The final blind README gate also corrected a mismatched LAN peer subnet,
removed an undeclared `uv` dependency from the retry proof, and documented
recoverable source-install rollback. A follow-up gate found missing bearer
headers in the token-protected federation variant and a linked setup guide
that still exposed auth keys in argv while leaving node-info on loopback. The
federation proof now distinguishes ACL-only from bearer-token calls, and the
setup guide uses interactive tagged enrollment plus an explicit Tailscale
node-info bind. The last activation review caught a producer-ownership gap:
Mesh adopts Ollama and therefore cannot create the configured private listener.
The guide now starts Ollama explicitly for foreground use and persists Ollama
and Mesh as separate processes on Linux, macOS, and Windows; Windows preserves
the tailnet/node-info arguments and identifies its Scheduled Task limitations.

The release gate on the clean branch passed 1,275 tests with 13 intentional
live-runtime/training skips and six documented training-stub warnings. Ruff,
18 strict catalog cards, both source/wheel builds, release artifact inspection,
Twine metadata checks, the Docker image, Compose rendering, and the exact
privacy/placeholder scan all passed.

Live verification from an untagged tailnet client reached the gateway through
Tailscale Serve, observed three reachable/routable specialists, and completed
a chat request through the canonical Ollama specialist. A specialist-tagged
node was correctly unable to act as a gateway client under the role policy.

The widened full-suite gate exposed a shutdown race in the new cleanup worker:
the router could cancel after an upstream delete succeeded but before SQLite
confirmation committed. A deterministic delayed-response regression now proves
shutdown signals the worker, lets one leased operation finish, and cancels only
after a bounded 40-second deadline. Five repeated restart-cleanup runs and the
full suite passed afterward.

### Additional artifacts

- `mesh/job_store.py` — durable job-owner cleanup state machine.
- `mesh/tests/test_video_job_store.py` — persistence and lease regressions.
- `mesh/tests/test_router_auth_security.py` — listener-token safety gate.
- `mesh/tests/test_video_request_security.py` — unsafe video-field rejection.
- `mesh/tests/test_supply_chain_pins.py` — immutable workflow/image pin gate.
- `docs/retros/LEDGER.md` — proposal to property-test SQLite state machines
  after the review loop exposed repeated transition defects.
