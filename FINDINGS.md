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
