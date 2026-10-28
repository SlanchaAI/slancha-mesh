# Review findings

Review base: `2bf3305` (main), branch `qc/burn1027-slancha-mesh`.

## Baseline and boundaries

- Locked Python 3.12 environment: core, tuning add-on, dev and signing extras.
- Baseline: **1,254 passed, 19 skipped, 6 warnings**, 14.55 seconds.
- Tests run with `nice -n 15`, one worker, numerical libraries limited to one thread.
- Hardware/model inference, deployment, paid services and external telemetry excluded.
- GitNexus MCP and CLI unavailable (`gitnexus: command not found`); direct source and caller inspection substitutes for unavailable index tooling. No index tool installed.

## Severity-ranked findings (investigation in progress)

### P1 — Authentication guard accepts unsafe listener configuration

Baseline evidence: `mesh/auth.py:164` treats an empty bind and any `127.*` hostname as loopback. An empty socket host is a wildcard bind; a hostname such as `127.attacker.example` need not resolve to loopback. Separately, baseline `mesh/cli.py:174` treats a whitespace-only token as configured while endpoint authentication strips it to empty.

**Fixed:** classify numeric loopback addresses structurally and reuse the central stripped-token check from `up`. Tests exercise the CLI startup caller without starting a server, reject empty/deceptive hosts, and retain legitimate IPv4/IPv6 loopback binds. Ten cases failed before the guard changes (including the private-range finding below).

### P2 — Private-address policy omits carrier-grade NAT addresses

Baseline evidence: `mesh/url_guard.py:93` checks only `ip.is_private`, which excludes `100.64.0.0/10`, despite the module's documented policy. Hardened deployments therefore still accept tailnet/CGNAT destinations with `SLANCHA_NODE_URL_BLOCK_PRIVATE=1`.

**Fixed:** include that exact range after canonicalizing numeric/mapped addresses. Tests cover both range endpoints, hexadecimal IPv4, mapped IPv6, default allowance, and adjacent public ranges.

Verification: focused authentication/CLI/URL/probe/registry API suite passed (133); changed-file lint and whitespace checks passed. Read-only caller review confirmed router startup already strips tokens and node startup now uses the same central policy. GitNexus change detection remains unavailable; explicit diff scope checked before commit.

### P2 — Failed heartbeat writes mutate registry identity and URL state

Baseline evidence: `mesh/registry.py:285` pins identity before `_record`; `mesh/registry.py:296` changes the URL before durable append. The durable-first contract at baseline `mesh/registry.py:255` promises failed appends leave the read model untouched. A failed first heartbeat could reserve an identity that was never committed; a failed update could publish an uncommitted URL.

**Fixed:** publish identity/URL state only after durable append. Regression tests reject the write, verify unchanged routing, and accept a subsequent valid identity.

### P2 — Restart/compaction loses remembered heartbeat URLs

Baseline evidence: `mesh/registry.py:249` replays pins but not `_node_urls`; `mesh/registry.py:307` persists only the current optional URL; snapshot construction relies on an in-memory remembered URL. Heartbeats may omit their URL after joining. A restart after such a heartbeat makes a live specialist unroutable.

**Fixed:** replay remembered URLs and persist the effective URL in each heartbeat so compaction cannot erase it. Tests cover ordinary and compacting stores, repeated restarts, and subsequent URL-less heartbeats.

### P2 — Durable quality observations do not rebuild catalog quality

Baseline evidence: `mesh/registry.py:249` replays events without applying quality observations; baseline `mesh/registry.py:363` updates only the running catalog. Restart resets measured quality and sample counts, changing routing decisions.

**Fixed:** replay quality observations into catalog cards, including accumulated sample count and observation source. Regression covers multiple observations, restart, and another observation afterward.

Verification for these three findings: five regression cases failed before fixes; focused registry/identity/API/concurrency tests passed (80); full suite passed (1,259 passed, 19 skipped, six existing stub-training warnings).

### P2 — Malformed peer metadata aborts discovery or creates bogus routes

Baseline evidence: `mesh/discovery.py:198` iterates an unchecked `data` value; baseline lines 212, 227 and 233 assume URL/capability lists and a quality mapping. Integer values raise; a URL string is iterated character by character and produces bogus pinned URLs. One peer can interrupt refresh for every specialist.

**Fixed:** check container shapes before parsing, discard malformed entries, and continue with valid entries/peers. Eight cases failed before fixing; expanded discovery tests now cover scalar model lists and malformed metadata alongside healthy models.

### P2 — Client errors permanently strand half-open circuit probes

Baseline evidence: `mesh/runtime_health.py:87` refuses every further request in `half_open`; baseline `mesh/runtime_health.py:129` counts client errors without releasing a probe. Chat/media upstream 4xx call that unkeyed method (baseline `mesh/router_app.py:1317`, `mesh/router_app.py:2842`). Once a recovering backend returns a request-specific 400, it stays unavailable indefinitely.

**Fixed:** carry the binding key for upstream client errors and close a responding half-open circuit without counting a successful inference. Local validation errors remain unkeyed. Streaming/non-streaming chat regressions failed before fixing; JSON/multipart media caller tests and a local-error isolation test expand coverage.

### P2 — Allocator assigns specialists to departed nodes

Baseline evidence: `mesh/registry.py:545` reconstructs hardware from heartbeats but ignores subsequent `NodeLeftEvent`, although snapshot replay removes those nodes. This can consume allocation coverage on a machine that left.

**Fixed:** remove departed hardware during allocator replay; a later heartbeat restores eligibility. Depart/rejoin regression failed before fixing.

Verification for discovery, circuit recovery and departed-node allocation: 11 cases failed before fixes; full suite now passes (1,291 passed, 19 skipped, six existing warnings). Changed-file lint and whitespace checks pass.

### P2 — Quality probes target the wrong endpoint and model name

Baseline evidence: `mesh/quality_probe.py:356` appends `/chat/completions` to an origin URL while backends expose `/v1/chat/completions`. Baseline line 508 sends the catalog's weight repository ID, although `mesh/backends.py:170` serves the specialist ID and `mesh/router_app.py:277` rewrites Ollama/external aliases. Healthy nodes can be recorded as zero-quality after HTTP errors.

**Fixed:** accept both origin and existing `/v1` bases, and use engine-served names in the CLI. Tests execute the real probe/CLI through a stubbed transport, inspect request paths and model names, and observe quality writes. No inference endpoint called.

### P2 — Judge parser silently accepts invalid numeric scores

Baseline evidence: `mesh/quality_probe.py:153` matches individual digits inside decimals, negatives and exponents; `42 out of 5` falls through to 5. This violates the stated integer-only contract and corrupts routing quality.

**Fixed:** tokenize the first number and require an exact 0–5 integer token. This is output syntax validation, not semantic grading or an adversarial-output defense. Existing supported prose remains accepted. Fourteen regressions across these quality-probe findings failed before fixes.

### P2 — Non-finite evaluation scores bypass promotion checks

Baseline evidence: `packages/slancha-mesh-tune/mesh/eval/gate.py:198` compares score deltas without checking finiteness. NaN comparisons are false, allowing a NaN challenger mean or domain score to produce ACCEPT. The CLI accepts JSON NaN; `loop_runner.py:471` invokes promotion on an accepting verdict.

**Fixed:** reject non-finite mean and domain scores on either side. Tests cover NaN and both infinities for both records and exercise the CLI's recorded rejection. No training or promotion executed.

### P2 — Malformed node ports pass ingest validation

Baseline evidence: `mesh/url_guard.py:87` parses URLs without reading the lazily validated `port`. Non-numeric/out-of-range ports enter routing state and can raise client URL errors during requests. Invalid IPv6 instead leaks a generic ValueError from a function promising NodeUrlError.

**Fixed:** force port validation and normalize parse errors to NodeUrlError. Regression covers malformed IPv6, non-numeric, negative and oversized ports.

### P2 — IPv6 LAN discovery produces invalid authorities

Baseline evidence: `mesh/discovery.py:153` and `mesh/discovery.py:321` interpolate raw peer hosts into URL authorities without IPv6 brackets. Explicit IPv6 LAN peers therefore cannot be fetched or routed.

**Fixed:** bracket bare IPv6 hosts at both seams; retain already bracketed hosts. Caller test runs synthesized LAN status through HTTP discovery and verifies both fetch and routed URLs.

### P2 — Independent holdout rounding changes the requested sample total

Baseline evidence: `packages/slancha-mesh-tune/mesh/eval/holdout.py:107` rounds every domain quota independently. With the default distribution, requesting one, two or three rows returns zero, one or two even with ample data; requesting 17 oversamples. Frozen evaluation size therefore differs from operator intent without a bucket shortfall.

**Fixed:** allocate fractional remainders together with deterministic tie order. Tests cover small/large odd sizes, exact totals, quota error below one and unique sampling; existing insufficient-bucket behavior remains covered.

### P2 — Invalid cluster-head confidence bypasses the routing gate

Baseline evidence: `mesh/classifier/cluster_head.py:187` checks only `conf < threshold`. NaN and positive infinity pass that check; probabilities above one also override the six-head domain classifier. The caller at `mesh/classifier/heads.py:197` then changes the routing domain.

**Fixed:** require confidence within the inclusive threshold–1 range. Tests execute the real domain-override caller with NaN, infinity and 1.01 and verify the original domain survives. NumPy 2.3.5 from the Python 3.12 lock resolution enables these tests without inference runtimes.

### P2 — Placement recommendations violate strategy and capacity constraints

Baseline evidence: `mesh/allocator.py:259` updates cluster coverage while computing supposedly independent `best_per_machine` recommendations, changing an identical node's selection when another node precedes it. Line 398 ranks the entire catalog during replica fill, bypassing the tier-three 5% traffic gate. Lines 482–488 check secondary memory but omit aggregate primary-plus-secondary storage, recommending 120 GB on a 100 GB disk.

**Fixed:** keep independent rankings independent, filter replica candidates through the existing traffic gate, and check combined storage. Four regressions failed first. Seven new cases cover ordering, traffic threshold and exact disk boundaries; the real registry allocator caller records an unassigned node when only a cold tier-three model is available. Focused allocator/registry verification: 52 passed.

### P2 — Zero available GPU memory is replaced with unrelated system RAM

Baseline evidence: `mesh/engine_select.py:61` treats zero free VRAM as missing, then returns host RAM. A discrete NVIDIA node with zero free GPU memory and 48 GB RAM is recommended vLLM as though 48 GB GPU memory were available.

**Fixed:** distinguish zero from None. Regression invokes the real onboarding `build_plan` caller with stubbed hardware/tailnet inputs and verifies the limited-memory recommendation and zero-GB rationale.

### P2 — Malformed peer tags abort discovery or select untagged hosts

Baseline evidence: `mesh/discovery.py:117` and `mesh/discovery.py:129` perform membership directly on unvalidated Tags values. Numeric values raise TypeError; strings use substring matching and dictionaries use keys, selecting malformed peer records as eligible specialists.

**Fixed:** require the JSON array shape before tag membership for Self and Peer. Ten discovery regressions failed before the fix; tests verify a valid peer still fetches and aggregates while malformed peers are ignored. Combined engine/discovery/CLI checks: 78 passed.

### P2 — Non-finite backend metrics interrupt heartbeats

Baseline evidence: `mesh/backends.py:317` accepts NaN/infinity as floats, then lines 338/342 convert them to integers. ValueError/OverflowError escapes `ServeDaemon.heartbeat` at `mesh/serve.py:196`, interrupting heartbeat updates. Non-finite cache ratios also leak into utilization output.

**Fixed:** ignore non-finite metric values. Twelve cases first failed through the real daemon heartbeat caller, using stubbed HTTP responses; ordinary valid metrics retain existing coverage.

### P2 — Malformed Ollama model lists escape best-effort polling

Baseline evidence: `mesh/backends.py:545` assumes an object containing a list of objects. Line 577 additionally builds a set from unchecked names. Wrong containers, scalar entries or unhashable names raise during heartbeat utilization or model-readiness checks.

**Fixed:** validate both container levels, skip malformed entries, and compare names without hashing untrusted values. Six regressions failed first, including a mixed list proving valid entries remain usable. Backend/daemon verification: 78 passed. All HTTP responses stubbed; no backend process started or stopped.

### P3 — Corrupt sidecar encoding blocks classifier startup

Baseline evidence: `mesh/classifier/cluster_head.py:220` reads UTF-8 but catches only OSError. Invalid UTF-8 escapes the documented inert-on-load-failure contract and reaches auto-router startup (`mesh/classifier/auto.py:122`).

**Fixed:** handle UnicodeError as a sidecar load failure. Four cluster regressions failed before these two fixes; only the optional selector and its startup loader are affected. GitNexus remains unavailable; callers and explicit diff reviewed directly.

### P3 — Malformed signatures escape the verifier error contract

Baseline evidence: `packages/slancha-mesh-tune/mesh/eval/row_sign.py:40` passes non-ASCII strings to `hmac.compare_digest`, which raises TypeError instead of returning False. `eval/seed_verify.py:100` catches only bad signature contents; a wrong-length Ed25519 signature raises ValueError instead of SeedVerificationError. Both fail closed, but callers receive unhandled exceptions rather than their documented rejection paths.

**Fixed:** reject non-ASCII HMAC values before comparison and normalize wrong-length signature failures. Four signature cases failed first; holdout tests exposed four additional failures.

## Module map

- Entry points: `mesh/cli.py`, `mesh/serve.py`, `mesh/service_install.py`, `mesh/router_bootstrap.py`.
- Hardware and placement: `probe.py`, `engine_select.py`, `allocator.py`, `gpu/`, catalog TOML cards.
- Discovery: `tailnet.py` → `discovery.py` → `router_app.discovery_to_snapshot`.
- Control plane: `registry_app.py` → `registry.py` → `event_store.py`; shared models in `models.py`.
- Data plane: `router_app.py` → `select.py`, `protocols.py`, `runtime_health.py`, backends; video ownership in `job_store.py`.
- Security: `auth.py`, `identity.py`, `url_guard.py`; route-specific auth and media validation.
- Optional classifier: lazy classifier assets and heads behind `model:auto`.
- Telemetry: `usage.py` constructs usage events and supports an optional local spool/drain sink.
- Optional tuning package: grading/curation/replay/eval/promotion/training/dashboard; excluded from core wheel.
