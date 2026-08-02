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

Pending deployment and end-to-end tailnet probes.
