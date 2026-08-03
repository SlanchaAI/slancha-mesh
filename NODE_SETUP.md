# Joining slancha-mesh as a specialist node

A how-to for the agent setting up a contributor's machine. Goal: get the contributor's box
serving a model the mesh can route to, with the least possible human touch.

## Safe quickstart

```bash
python -m pip install -e .         # from source; not yet on PyPI
slancha-mesh plan --json           # stop if no catalog card fits
ollama pull qwen3:14b              # 9.3 GB; canonical live-validated card
sudo tailscale up --advertise-tags=tag:specialist
# Complete browser login/admin approval, then use two terminals.
```

Terminal A owns the private Ollama listener. Quit any Ollama tray/menu-bar
process already listening on loopback before starting it:

```bash
TAILSCALE_IP=$(tailscale ip -4)
OLLAMA_HOST="$TAILSCALE_IP:8003" ollama serve
```

Terminal B starts Mesh against that exact origin:

```bash
TAILSCALE_IP=$(tailscale ip -4)
SLANCHA_AUTH_REQUIRED=false OLLAMA_HOST="$TAILSCALE_IP:8003" \
  slancha-mesh up --tailnet --specialist qwen3-14b-q4-ollama \
  --node-info-host "$TAILSCALE_IP"
```

The interactive Tailscale join keeps credentials out of shell history and
process arguments. Ollama produces the model endpoint; `up` adopts and health
checks it, then exposes pull discovery on the Tailscale address. **No registry
URL, node token, per-node gateway config, or secret exchange between machines
is required.** The ACL is the credential; the explicit auth opt-out is safe
only behind the policy below.

Use `--auto` only after `plan --json` confirms the chosen card and its engine
are installed; use repeatable `--specialist <id>` for explicit models. Run
either form with `--dry-run` first to preview without starting anything.

## The trust model (why it's this simple)

Tailnet membership **is** the credential. A node tagged `tag:specialist` on
the tailnet is, by the ACL (`tag:gateway -> tag:specialist:<ports>`,
deny-by-default), reachable by the gateway and nothing else. Discovery is
**pull-based**: the gateway walks its own `tailscale status` peer list and
fetches each specialist node's `/models` over the tailnet. Because it pulls a
node's description *from that node's own address*, a node can never advertise
another node's address — identity is the address. So there's no heartbeat
token to hand out and no way to hijack routing by lying.

A node leaves the mesh simply by leaving the tailnet (or stopping `up`) — it
drops off the next discovery pass. Spinning specialists up and down is just
starting and stopping the process.

## What you need from the gateway operator (once)

1. **Permission to advertise `tag:specialist`.** Prefer the interactive join
   above. For unattended headless enrollment, use the control plane's secret
   file/config mechanism; do not pass a raw auth key to `slancha-mesh --key`
   or commit it to a unit, script, shell history, or environment file.
2. **The ACL grant exists for the ports you serve** —
   `tag:gateway -> tag:specialist:8003,8004` (model ports) **and `:8088`**
   (the node-info / discovery port the gateway pulls). This is a one-time ACL
   entry, not per node. The routability invariant follows: advertise your
   model URL on an ACL-opened port (`:8003`/`:8004`) or the node is "up but
   unroutable" — see the
   [port convention](README.md#port-convention--the-routability-invariant).
3. **Headscale only:** the `--login-server=https://<host>` URL → pass
   `--control-plane headscale --login-server <url>` to `up`.

## What `slancha-mesh up` does, step by step

1. **Verify tailnet membership** (idempotent). Join interactively with the tag
   before `up`; an absent or unapproved membership fails loudly instead of
   producing a silent half-state.
2. **Resolve the MagicDNS advertise host** the gateway will dial.
3. **Pick specialists** — `--auto` fits the catalog to the probed hardware
   (VRAM/backend/throughput); or explicit `--specialist`.
4. **Adopt or launch backends** according to engine ownership. Ollama must
   already be listening at `OLLAMA_HOST`; Mesh adopts it and never stops it.
5. **Expose self-description** — the explicit `--node-info-host` above runs
   node-info on the Tailscale address at :8088, serving live
   `/models?include=routing_meta` with each
   specialist's MagicDNS `node_url`. This is what the gateway pulls.

Check it from the box: `slancha-mesh status` (shows tailnet identity +
whether the box is `specialist-ready`).

## Verify it's live

```bash
# On the box:
slancha-mesh status                    # online + tag:specialist present?

# From any tag:gateway / admin device — discover what the mesh sees:
slancha-mesh discover                  # table of specialists → host-pinned node_urls
# the box should appear with node_urls like http://<its-magicdns>:8003

# End-to-end through the gateway:
curl https://api.slancha.ai/v1/chat/completions \
  -H "Authorization: Bearer slancha_<bearer>" -H "Content-Type: application/json" \
  -d '{"model":"<specialist-id>","messages":[{"role":"user","content":"hi"}]}'
```

## Running it as a service (survives reboots)

`slancha-mesh service` makes the Mesh process boot-persistent. Ollama is a
separate producer and must also be configured to start on the same private
origin. Pass args for `up` after a literal `--`; with none it defaults to the
loopback-oriented `up --auto`, which is not a multi-box node.

```bash
TAILSCALE_IP=$(tailscale ip -4)
slancha-mesh service install --kind node \
  --env SLANCHA_AUTH_REQUIRED=false \
  --env OLLAMA_HOST="$TAILSCALE_IP:8003" \
  -- --tailnet --specialist qwen3-14b-q4-ollama \
  --node-info-host "$TAILSCALE_IP"
slancha-mesh service install --kind router --role router -- --peer spark.example.ts.net --auto-route
slancha-mesh service install --dry-run             # print the unit/plist/task, touch nothing
slancha-mesh service status
slancha-mesh service uninstall
```

The `--role` suffix names the artifact (`ai.slancha.mesh.<role>`) so one box
can host several services; it defaults to the selected `--kind`. Repeat
`--env NAME=VALUE` for non-secret
settings that must survive reboot. It is supported by systemd and launchd,
not Windows Scheduled Tasks. Never put a token in `--env`: the rendered
service definition is stored on disk.

On an intentionally isolated trusted network only, persisting
`SLANCHA_AUTH_REQUIRED=false` disables the non-loopback authentication guard.
Prefer a node token or tailnet ACL; never use this opt-out on an untrusted LAN.

### Linux — systemd `--user` unit

First persist Ollama's listener in its system service, following Ollama's
documented override mechanism:

```ini
# sudo systemctl edit ollama.service
[Service]
Environment="OLLAMA_HOST=100.64.0.10:8003"
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
curl --fail http://100.64.0.10:8003/api/tags
```

Replace `100.64.0.10` with this node's stable Tailscale address in both the
Ollama override and Mesh unit.

`install` writes `~/.config/systemd/user/ai.slancha.mesh.<role>.service` and
runs `systemctl --user enable --now`. `--kind node` wraps `up`; `--kind
router` wraps `router`. Equivalent to the hand-written node unit:

```ini
# ~/.config/systemd/user/ai.slancha.mesh.node.service
[Unit]
Description=slancha-mesh node (node)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
Environment=SLANCHA_AUTH_REQUIRED=false
Environment=OLLAMA_HOST=100.64.0.10:8003
ExecStart=/home/you/.local/bin/slancha-mesh up --tailnet --specialist qwen3-14b-q4-ollama --node-info-host 100.64.0.10
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

(Headless boxes may need `loginctl enable-linger $USER` for the `--user` unit
to start before login.)

### macOS — launchd LaunchAgent

`install` writes `~/Library/LaunchAgents/ai.slancha.mesh.<role>.plist` and runs
`launchctl load`. `RunAtLoad` starts it at login; `KeepAlive` restarts it on
crash (the launchd analog of `Restart=on-failure`).

Disable Ollama's GUI Login Item, then install a separate LaunchAgent for
`ollama serve`. Replace both placeholders; `command -v ollama` supplies the
absolute executable path:

```xml
<!-- ~/Library/LaunchAgents/ai.slancha.ollama.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.slancha.ollama</string>
  <key>ProgramArguments</key><array>
    <string>/absolute/path/from-command-v-ollama</string><string>serve</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>OLLAMA_HOST</key><string>100.64.0.10:8003</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
```

```bash
plutil -lint ~/Library/LaunchAgents/ai.slancha.ollama.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.slancha.ollama.plist
curl --fail http://100.64.0.10:8003/api/tags
```

Then install Mesh with the same address:

```bash
TAILSCALE_IP=$(tailscale ip -4)
slancha-mesh service install --kind node \
  --env SLANCHA_AUTH_REQUIRED=false \
  --env OLLAMA_HOST="$TAILSCALE_IP:8003" \
  -- --tailnet --specialist qwen3-14b-q4-ollama \
  --node-info-host "$TAILSCALE_IP"
# verify:
launchctl list ai.slancha.mesh.node
```

### Windows — ONSTART Scheduled Task

The built-in Scheduled Task cannot persist environment settings and does not
restart a failed process, so it is insufficient for this Ollama topology. In
an Administrator PowerShell, set machine-level variables, then quit/restart
Ollama:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_HOST', '100.64.0.10:8003', 'Machine')
[Environment]::SetEnvironmentVariable('SLANCHA_AUTH_REQUIRED', 'false', 'Machine')
```

The corrected Mesh task below preserves the required arguments, but it remains
a login-dependent deployment because the Ollama app starts at user login:

```
slancha-mesh service install --kind node -- --tailnet --specialist qwen3-14b-q4-ollama --node-info-host 100.64.0.10
```

Ollama's Windows app reads user/system environment variables and starts at
login. For inference before login or service-grade restart, install
[`nssm`](https://nssm.cc) and manage both producers explicitly:

```
nssm install slancha-ollama "C:\path\to\ollama.exe" serve
nssm install slancha-mesh "C:\path\to\slancha-mesh.exe" up --tailnet --specialist qwen3-14b-q4-ollama --node-info-host 100.64.0.10
nssm start slancha-ollama
nssm start slancha-mesh
```

`slancha-mesh service uninstall` removes the task (`schtasks /Delete`); for the
nssm path use `nssm remove slancha-mesh`.

## Don'ts (silent-failure traps `up` already guards against)

- **Don't hand-run `tailscale up` without `--advertise-tags=tag:specialist`.**
  Wrong/missing tag = on the tailnet but invisible to the gateway. `up` always
  sets the tag; `status` flags a missing one.
- **Don't leave node-info on `127.0.0.1`.** The gateway is off-box; pass the
  Tailscale address through `--node-info-host` as shown above. `up --tailnet`
  binds managed model backends for tailnet reachability and advertises
  MagicDNS.
- **Don't expose ports publicly** (no Funnel/port-forward). The tailnet ACL is
  the only path in.
- **Don't claim capabilities the backend lacks** in a card — the router gates
  on them. `--auto` only serves cards already in the catalog.

## When it breaks

| Symptom | Cause / fix |
|---|---|
| `up` exits "not on the tailnet… run: tailscale up …" | Not joined. Run the interactive tagged join above and obtain admin approval. |
| `status` shows `specialist-ready: False` | Missing `tag:specialist`. Re-run the interactive join with the tag or ask the tailnet admin to approve it. |
| `discover` doesn't list the box | Not online, or node-info :8088 not in the ACL for `tag:gateway`. |
| `discover` lists it but routing fails | Model port (8003/8004) not in the ACL, or backend didn't come up — check `up` logs. |
| `up --auto` says "nothing fits this box" | No catalog card passes the hardware filter (VRAM/backend). Add a fitting card or use `--specialist`. |

## Under the hood / OSS

`slancha-mesh discover` is the same library call (`mesh.discovery.
discover_specialists`) the slancha-api gateway uses to build its routing
table — so an external OSS adopter with their own tailnet gets a working mesh
with zero central infrastructure, and the cloud is just one consumer of that
discovery path. Full design + the push-vs-pull decision:
`docs/SELF_ORGANIZING_LOOP_SCOPE.md`.
