# Onboarding a new specialist (or a new self-hoster)

The v0.1 substrate is "drop a card → register → restart." This walk-through
covers both cases — Paul adding a new specialist to his own mesh node,
and someone-not-Paul standing up their first mesh node.

## Case A: new specialist on an existing mesh node

You already have a running `slancha-local` + `vLLM` (or `llama.cpp` /
`mlx-lm` / `ollama`) serving an OpenAI-compatible endpoint. You trained
or downloaded a new LoRA / merged model. Goal: get it visible in the
mesh registry so the router can pick it.

1. **Write the card TOML.** Drop a file at `mesh/catalog/<your-id>.toml`:

   ```toml
   [specialist]
   model_id            = "vendor/base-model-name"
   specialist_id       = "your-id"               # local handle, e.g. "voice-essay-v9"
   domain              = "writing"               # writing | code | math | reasoning | general | multilingual
   difficulty_tiers    = ["easy", "medium", "hard"]
   languages           = ["en"]
   required_backend    = "vllm"
   storage_gb          = 24.0                    # weights on disk
   runtime_gb          = 30.0                    # weights + KV cache at runtime budget
   min_vram_gb         = 16.0
   context_window      = 32768
   n_layers            = 32

   # Phase 5 — what agents passing X-Slancha-Pref will gate on
   capabilities        = ["streaming", "system_prompt", "tools"]

   # Phase 6 — node-self-reported quality (DISPLAY-ONLY for routers
   # by default — they trust router_observed once probes run)
   quality_node_self_reported = 4.0
   ```

2. **Restart the mesh registry** so it auto-loads the new TOML:
   ```bash
   systemctl --user restart mesh-registry.service
   ```

3. **Confirm registration:**
   ```bash
   curl -s -H "Authorization: Bearer $SLANCHA_NODE_TOKEN" \
     http://localhost:8088/registry | jq '.snapshot.catalog | keys'
   ```
   You should see `"your-id"` in the catalog list.

4. **Load the model** in your serving backend (`vllm serve ...` with
   `--lora-modules your-id=/path/to/lora`). The heartbeat client picks
   up `loaded_models` on the next 5-second tick.

5. **Confirm the binding** is visible:
   ```bash
   curl -s -H "Authorization: Bearer $SLANCHA_NODE_TOKEN" \
     http://localhost:8088/models?include=routing_meta | jq
   ```
   The new specialist appears in `data[]` with `node_urls` populated.

6. **Start the quality probe** (one-shot or via cron):
   ```bash
   python -m mesh.quality_probe \
     --base-url http://localhost:8088 \
     --token $SLANCHA_NODE_TOKEN
   ```
   This sends a probe set, writes `quality_router_observed` back into
   the card. Drift events emit to the `mesh.quality` logger.

Total time: ~5 minutes once the model is loaded.

## Case B: standing up a new mesh node from scratch

Imagine you're not Paul. You have a beefy local machine, you want to
join the mesh, and contribute a specialist. The substrate steps:

### One-time mesh-node setup

1. **Install slancha-local + slancha-mesh** on your box. Pull both
   repos, install requirements, run the heartbeat loop.

2. **Boot the mesh registry** locally:
   ```bash
   SLANCHA_NODE_TOKEN=$(openssl rand -hex 32) \
   uvicorn mesh.service:create_mesh_app --factory --port 8088
   ```

3. **Stand up a Cloudflare Tunnel** pointing at your local
   slancha-local OpenAI-compat endpoint. Convention:
   `<handle>-mesh.example.com` (single-level subdomain — CF
   Universal SSL covers it without an advanced certificate). See
   `~/.cloudflared/config.yml` template in `infra/cloudfront/README.md`.

4. **Issue a CF Access service token** scoped to the new tunnel.
   Operator (Paul) does this — adds your tunnel's hostname to the
   Access policy + emits the `CF-Access-Client-Id` /
   `CF-Access-Client-Secret` pair via wire.

### One-time SaaS-side setup (paul-mac does this for you)

5. **Add your origin to the L@E allowlist.** Paul edits
   `infra/cloudfront/origin_request_lambda.py`'s
   `MESH_ORIGIN_REGISTRY` to include `<handle>-mesh-laulpogan-com →
   <handle>-mesh.example.com`. Rebuild the L@E zip; reapply
   Terraform `-target=module.mesh_lambda_edge`.

6. **Seed your KVS record.** Paul runs:
   ```bash
   python infra/cloudfront/kvs_seed.py \
     --kvs-arn $MESH_ROUTE_KVS_ARN \
     --bearer slancha_<your-bearer> \
     --user-id <your-supabase-uuid> \
     --route-target mesh \
     --mesh-origin-id <handle>-mesh-laulpogan-com \
     --pref-max '{"max_cost_cents": 100, "max_latency_ms_p95": 5000}'
   ```
   This populates the KVS with your bearer-hash → routing record so
   CloudFront knows to send your traffic to your tunnel.

### Per-specialist (you again, repeated for each model)

7. **Follow Case A above** to register specialists on your node.

### Validation

8. **Smoke test from a SaaS-shape client:**
   ```bash
   curl https://api.slancha.ai/v1/chat/completions \
     -H "Authorization: Bearer slancha_<your-bearer>" \
     -H "Content-Type: application/json" \
     -d '{"model": "your-id", "messages": [{"role":"user","content":"hi"}]}'
   ```
   Expect SSE chunks streaming back via your tunnel. The response
   carries `X-Slancha-Route-Target: mesh` if you watched headers.

## Anti-patterns this onboarding prevents

- **DON'T copy/paste an existing TOML** without auditing `domain` and
  `capabilities`. The router gates on these — wrong tags break routing.
- **DON'T set `quality_router_observed` directly.** That field is
  written by the central probe service. Self-reported is `quality_node_self_reported`.
- **DON'T claim capabilities your backend doesn't support.** A request
  with `require_capabilities=["tools"]` will be sent to you; if you
  don't actually do tools, it's a routing failure.

## Where to look when things break

- Heartbeat not registering: `GET /registry` shows your node? If no,
  check `SLANCHA_NODE_TOKEN` env on both ends.
- Specialist registered but no traffic: `GET /models?include=routing_meta`
  — capabilities listed? router_observed populated?
- Tunnel reachable but 401: CF Access service-token in the L@E
  customHeaders matches the tunnel's expected pair.
- Forward-sig mismatch: HMAC key rotation — both L@E and slancha-local
  must hold the same key (KID-aware grace window).
