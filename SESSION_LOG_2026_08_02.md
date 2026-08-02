# Session log — 2026-08-02

## Tailscale fleet deployment

Goal: make the OSS routing stack durable and reachable over Paul's private
tailnet, using existing model servers without downloading weights or taking
over their lifecycle.

### Facts before change

- Mac `pauls-macbook-pro.taila93596.ts.net` is `tag:paul-host`.
- Dell and `promaxgb10-d325` are online with `tag:specialist`; existing grants
  already cover the mesh ports, so no ACL edit is required.
- Mac Mesh router already polls Spark, Dell, and GB10 every 30 seconds.
- Only Spark exposed node-info on `:8088`, so the live registry had one route.
- vLLM Semantic Router launchd supervisor was running, but Docker Desktop was
  stopped. After Docker returned, the container stack needed more than the
  first readiness window to construct; launchd itself was healthy.
- Dell already serves `dot-backbone` on `:8011`.
- GB10 already serves `Qwen/Qwen3-VL-8B-Instruct-FP8` as `qwen3-vl-8b` on
  loopback `:8901`; a live 8-token marker completed in 0.70 seconds.

### Decision

Adopt existing endpoints through external-backend cards. Keep Spark on the
easy/general lane, GB10 on medium plus explicit vision, and Dell on hard. This
balances useful work without another resident model or an unobservable shared
capacity grab. Publish the front door through Tailscale Serve; do not bind it
publicly or use Funnel.

### Artifacts

- `mesh/catalog/qwen3-vl-8b-fp8-gb10.toml` — adopted GB10 vision-capable lane.
- `SESSION_LOG_2026_08_02.md` — deployment decisions and live evidence.

### Verification

- Full suite after adding the GB10 card: 1,137 passed, 16 skipped.
- Dell, GB10, and Spark run enabled user-systemd units with linger enabled;
  current `systemctl --user is-active` / `is-enabled` checks returned
  `active` / `enabled` for all three node units and the GB10 `:8003 -> :8901`
  forward. `loginctl show-user` returned `Linger=yes` on every Linux node.
- The Mac router and semantic-router LaunchAgents are running and restart at
  user login. Docker Desktop, which supplies the semantic-router containers,
  is also a macOS login item. This is login persistence, not a pre-login
  machine service.
- Mac Mesh router discovers three routable specialists.
- Tailscale Serve exposes raw TCP `:8888` to loopback `:8888`; HTTPS Serve is
  unavailable until the tailnet-wide HTTPS certificate toggle is enabled.
- An Orin (`tag:paul-host`) request reached the front door through Tailscale,
  then vLLM Semantic Router, Mesh, and Spark. Response headers named the
  selected specialist and node. Spark's cold load took 29 seconds because
  Ollama requested a 262K context and evicted a resident model; the warm call
  returned HTTP 200.
- The original hand-written Mac router unit pointed at a virtual environment
  that had later been recreated without classifier dependencies. Replaced it
  with the generated router service backed by a validated Python 3.11
  classifier environment.

### Routing correction

Discovery alone did not balance medium work: Spark and GB10 both claimed the
medium tier, neither reported p95, and stable tie order kept choosing Spark.
Split the general lane explicitly: Spark easy, GB10 medium, Dell hard. Deploy
the same commit to Spark so its node-advertised card metadata carries the
policy; the Mac router does not override remote claims.

The first medium probe selected GB10 correctly but timed out because its vLLM
binds loopback `:8901`; discovery host-pinned that unreachable port. Reused the
box's existing root-owned `mesh-forward@8003` tailnet listener and added a
durable user-systemd inner hop from loopback `:8003` to `:8901`. The card now
advertises convention port `:8003`, preserving the vLLM loopback bind and ACL.

### Final live gate

From Orin (`tag:paul-host`), through Tailscale TCP `100.96.234.16:8888` and
the production `MoM` endpoint:

- easy: `HTTP/1.1 200 OK`, `x-slancha-specialist:
  qwen3-14b-q4-ollama`, `x-slancha-node:
  spark-472e.taila93596.ts.net:11434`;
- medium: `HTTP/1.1 200 OK`, `x-slancha-specialist:
  qwen3-vl-8b-fp8-gb10`, `x-slancha-node:
  promaxgb10-d325.taila93596.ts.net:8003`;
- hard: `HTTP/1.1 200 OK`, `x-slancha-specialist:
  qwen3.6-27b-fp8-dot`, `x-slancha-node:
  dellpromax.taila93596.ts.net:8011`.

Router health reported three reachable/routable specialists, three closed
runtime circuits, three successes, zero failures, and zero punts. Tailscale
Serve retained the pre-existing `:8772` listener and added only private TCP
`:8888 -> 127.0.0.1:8888`.

Live ACL retrieval confirmed both `tag:paul-host` and `tag:gateway` can reach
`tag:specialist` on `8003`, `8011`, `8088`, and `11434`; the gateway grant also
has deny-by-default self-tests. `tagOwners` restricts `tag:specialist` and
`tag:gateway` to Paul's admin identity. No ACL edit was needed.

Remaining seams: HTTPS needs the tailnet-wide certificate toggle; the current
TCP proxy remains WireGuard-encrypted, ACL-filtered, and tailnet-only. Cloud
punt consumption is not configured in the deployed vLLM policy; local
exhaustion returns the typed punt for an upstream cloud executor to consume.

## Multimodal adapters and private media front door

Goal: expose the new trusted image/audio/edit/transcription/video routes for
personal use without adding a heavyweight runtime to Mesh core or weakening
the existing chat path.

### Runtime examples

- Added pinned LocalAI 4.7.1 container config, protocol map, conservative
  external-card template, and curls for image generation/edit, text to speech,
  transcription, and inline synchronous video.
- Added pinned vLLM-Omni 0.20.0 startup guidance, protocol map, conservative
  external-card template, and curls for image generation/edit, audio, and the
  durable asynchronous video lifecycle.
- Both cards tell operators to replace model identity, immutable revision where
  applicable, license, and measured capacity. Each advertises one conservative
  protocol; operators must add a token only after proving that endpoint on the
  configured model.
- Neither runtime, CUDA, media codecs, nor weights became a core dependency.
  Primary references are linked from `examples/multimodal/README.md`.

No LocalAI or vLLM-Omni process/container was present on the six reachable
SSH-capable fleet hosts. No runtime was installed and no weights were
downloaded. Status: adapters built, not wired; missing producer is an
operator-configured LocalAI or vLLM-Omni specialist service.

### Real request and loopback evidence

A tiny inline PNG request used the OpenAI vision-chat shape through the live
Mac Mesh router. The body and model output were discarded. Bounded result:

- HTTP 200;
- `X-Slancha-Specialist: qwen3-vl-8b-fp8-gb10`;
- `X-Slancha-Node: promaxgb10-d325.taila93596.ts.net:8003`;
- selection reason `primary`, with queue depth zero.

Real-TCP tests now put runtime-shaped upstream and router applications on
loopback sockets. They inspect trusted method/path routing, JSON and multipart
content types, binary audio, upstream-only authorization, model aliasing, and
file bytes. The asynchronous video test creates a job, restarts the router with
the non-owner ranked first, then polls, downloads, and deletes through the
persisted owner. The decoy node receives zero calls.

### Tailscale Serve change

Before mutation, private TCP Serve contained only `:8772` and chat `:8888`.
Using the installed 1.98 CLI syntax, the additive command was:

```bash
tailscale serve --bg --tcp=8080 tcp://127.0.0.1:8080
```

After mutation, Serve contains `:8080 -> 127.0.0.1:8080`, `:8772`, and
`:8888`; each listener reports `tailnet only`. Funnel was not enabled and Mesh
still binds loopback. Rollback removes only the new listener:

```bash
tailscale serve --tcp=8080 off
```

From Orin, short MagicDNS resolution was unavailable because that host does not
accept tailnet DNS. Resolving the Mac peer through Orin's local Tailscale
netmap, without recording its address, proved both endpoints over the private
Serve listener:

- `/health`: HTTP 200, status `ok`, three reachable and three routable;
- `/v1/models`: HTTP 200, four entries, Qwen3-VL specialist present.

Status: direct media front door wired and live. Chat remains on vLLM Semantic
Router `:8888`. Paid/cloud handoff remains caller-owned and is not hidden inside
either path.

### Verification

- Runtime example/real-socket focus: 5 passed; Ruff clean; Docker Compose and
  both JSON protocol maps parsed.
- Router/media/video regression packet: 137 passed.
- Strict catalog validation: 18 cards clean.
- Full suite: 1,223 passed, 16 skipped, six pre-existing training-stub
  warnings.
- Whole core/add-on Ruff gate: clean.
- Release artifacts: two wheels plus two source distributions built; archive
  inspection passed all four; `twine check` passed all four; isolated uv-based
  installs proved core excludes training and the add-on composes at version
  `0.1.0a1`.

### Artifacts

- `examples/multimodal/README.md` — optional runtime boundary, startup, and
  adopted endpoint calls.
- `examples/multimodal/localai/` — pinned container, protocol map, card
  template.
- `examples/multimodal/vllm-omni/` — pinned runtime map and card template.
- `mesh/tests/test_multimodal_examples.py` — mapping/card drift gate.
- `mesh/tests/test_router_live_socket.py` — runtime-shaped wire and durable
  owner-pin proof.
- `examples/oss-routing/README.md` — alpha split between chat `:8888`, media
  `:8080`, and caller-owned cloud policy.
