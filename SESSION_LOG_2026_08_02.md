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
- Dell and GB10 run enabled user-systemd units with linger enabled; both pass
  node-info and tailnet-readiness doctor checks.
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
