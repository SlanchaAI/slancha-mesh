# Multimodal Mesh, Cloud Handoff, and Release Completion Plan

**Goal:** Finish the pushed OSS candidate as a reproducible alpha release,
extend the private-fleet router across bounded multimodal protocols, preserve
durable ownership for asynchronous media jobs, and prove the typed cloud punt
contract can be consumed without authorizing paid traffic.

**Implementation branch:** `build/oss-ready-core-split-impl`

## Global constraints

- Preserve the live path: vLLM Semantic Router `:8888` → Mesh `:8080` →
  Spark/GB10/Dell over Tailscale.
- vLLM Semantic Router v0.3 remains the chat front door. New media protocols
  use Mesh `:8080` directly until the upstream router supports and proves
  those paths; do not imply the outer router forwards them.
- Keep `POST /v1/chat/completions` behavior and rollback path compatible.
- Do not reshape `SpecialistCard` or `NodeBinding`; GitNexus rates both shared
  schemas CRITICAL. Reuse additive `capabilities` strings.
- Protocol IDs map to code-owned methods, paths, encodings, and response
  types. Node metadata never supplies an arbitrary upstream path.
- New multimodal endpoints require an explicit model in the first release.
- Non-idempotent media creation attempts one node only.
- Mesh never chooses a paid provider or reads a cloud credential. A caller
  owns punt authorization and one-shot execution.
- No paid API call, heavyweight engine install, model download, PyPI publish,
  GitHub merge, or tag occurs before its local deterministic and live gates.
- Use `apply_patch`; preserve unrelated work; commit atomic slices and push.

## Task 1 — Reproducible release artifacts

**Files:** `pyproject.toml`, `mesh/__init__.py`,
`packages/slancha-mesh-tune/pyproject.toml`, `CHANGELOG.md`, `.github/workflows/ci.yml`,
new `scripts/check_release_artifacts.py`, focused tests if useful.

- Move both packages from the colliding `0.0.6` to `0.1.0a1`; keep the tune
  dependency exact.
- Exclude local virtual environments, caches, tests, corpora, session logs,
  and add-on sources from the core source distribution.
- Build both wheels and source distributions in CI.
- Structurally inspect every archive for forbidden paths and required legal
  files; run `twine check` and clean-environment installs from artifacts.
- Prove a local `.venv-router` cannot contaminate the source distribution.

## Task 2 — Typed protocol registry and JSON media routes

**Files:** new `mesh/protocols.py`, `mesh/router_app.py`, new
`mesh/tests/test_multimodal_router.py`.

- Define trusted protocol IDs and adapters for:
  `openai.images.generations.v1`, `openai.audio.speech.v1`,
  `vllm_omni.audio.generate.v1`, and `localai.video.v1`.
- Use capability tokens `protocol:<id>` on existing cards.
- Add public routes for image generation, speech, general audio generation,
  and synchronous LocalAI video.
- Preserve explicit model alias rewriting, caller credential stripping,
  configured node credential injection, runtime circuits, bounded bodies,
  retry rules, typed punts, and routing audit headers.
- Accept only endpoint-specific JSON and expected response media types.
- Require LocalAI video `response_format=b64_json` in this release so relative
  node-local asset URLs never escape.
- Run focused router, auto-router, usage, live-socket, and full tests.

## Task 3 — Multipart input and binary output

**Files:** `pyproject.toml`, `mesh/protocols.py`, `mesh/router_app.py`,
`mesh/tests/test_multimodal_router.py`.

- Add bounded structural multipart handling for image edits and audio
  transcription; never regex multipart bodies.
- Forward fields/files without forwarding caller authorization.
- Add endpoint-specific media limits and expected binary response types.
- Add `openai.images.edits.v1` and `openai.audio.transcriptions.v1`.
- Prove oversized multipart, missing model, capability mismatch, unexpected
  media type, redirects, and retriable upstream failures fail safely.

## Task 4 — Durable vLLM-Omni video ownership

**Files:** new `mesh/job_store.py`, `mesh/router_app.py`, `mesh/cli.py`, new
`mesh/tests/test_video_job_store.py`, `mesh/tests/test_multimodal_router.py`.

- Store opaque public job ID → protocol, specialist, owner node, upstream job
  ID, timestamps, expiry, and last status in SQLite WAL mode.
- Default the DB beneath the router state directory; allow an explicit path.
- Implement create/status/content/delete for `/v1/videos`.
- Attempt create once; save ownership before returning; rewrite IDs.
- Poll, download, and delete only through the recorded owner. Owner outage
  returns a retryable typed 503 and never creates or selects another job.
- Proxy content bytes; never expose upstream-relative URLs or a global job
  listing.
- Restart the app/store in a real-socket test and prove ownership survives.

## Task 5 — Optional runtime examples and in-situ proof

**Files:** `examples/multimodal/`, `examples/oss-routing/README.md`, tests and
`SESSION_LOG_2026_08_02.md`.

- Add truthful adopted-endpoint examples for LocalAI and vLLM-Omni without
  making either a core dependency.
- Prove JSON, multipart, binary, and async job paths through real loopback
  sockets with runtime-shaped stubs.
- Send one real image-bearing chat request through the deployed Qwen3-VL route
  over the production Tailscale path; record selected specialist/node.
- Publish direct Mesh `:8080` through tailnet-only Tailscale Serve for media
  clients, without Funnel or a public interface, and prove the listener from a
  second tailnet device.
- If an already-installed LocalAI or vLLM-Omni runtime exists, run a direct
  optional probe. Do not install or download one.

## Task 6 — Caller-owned punt consumption

**Repository:** `barkeep`, on its own non-default branch and plan.

- Add a structured, no-prompt authorization decision that preserves
  `--no-spend` / `BARKEEP_NO_SPEND`, Prost budget policy, provider choice, and
  an API-only lane restriction.
- Add a thin caller adapter that accepts only a pinned Slancha origin and an
  exact valid typed punt, requires explicit caller consent, preserves the
  original OpenAI body, rewrites only the selected cloud alias, strips local
  credentials, and calls one configured gateway exactly once.
- Configure the test executor with retries and provider fallback disabled.
- Prove `BARKEEP_NO_SPEND=1` produces zero gateway calls; a simulated approved
  lane produces exactly one loopback call; malformed punts, ordinary 503s,
  vetoes, and gateway failures never trigger another call.
- Keep live metered traffic disabled pending explicit API-spend opt-in.

## Task 7 — OSS Ready README and release gate

**Mutable target during OSS Ready polish:** root `README.md` only.

- Run isolated blind visitor and evidence/clean-room critiques.
- Lead with the confirmed pitch: Tailscale-native heterogeneous inference,
  weakest-sufficient local routing, and a typed handoff to caller-owned cloud
  policy.
- Correct required ACL ports, catalog proof, network exposure language,
  prerequisites, package state, and reproducible claims.
- Validate links, narrow rendering, source install, artifact install, first
  value, rollback, license, security, and support paths.
- Re-run the blind critique; READY requires every hard gate verified.

## Task 8 — Final gates and publication operations

- Run GitNexus `detect-changes` against `origin/main`, focused tests, full
  suite, Ruff, strict catalog validation, both artifact builds, artifact
  inspection, clean installs, and real caller probes.
- Run one cross-provider fresh-eyes review; fix all BLOCKER/MAJOR findings.
- Push every atomic commit and wait for PR checks.
- Merge only after all branch gates are green. Create an immutable
  `v0.1.0a1` tag and GitHub prerelease only from the merged release commit.
- Configure branch protection and PyPI Trusted Publishing when available.
  If PyPI project ownership or tailnet HTTPS admin state is unavailable,
  report the exact external blocker; do not fabricate completion.

## Acceptance criteria

1. Existing chat traffic remains green locally and over Tailscale.
2. Image, speech, audio, multipart edit/transcription, and synchronous video
   requests select only a capable explicit specialist and preserve bytes.
3. Async video ownership survives router restart and never changes nodes.
4. Typed punts have a real caller consumer with a proven zero-spend veto and
   exactly-one-call approved path.
5. Core and tune artifacts are uncontaminated, independently installable, and
   version-aligned at `0.1.0a1`.
6. README launch gate is READY or names only external admin/credential
   blockers with every repository-local P0/P1 repaired.
7. PR checks and independent review contain no open BLOCKER or MAJOR.

## Rollback

- Existing chat routing stays on its current handlers and ports throughout.
- New protocol routes disappear by reverting their atomic commits; legacy
  cards without protocol tokens remain chat-only.
- The video job database is additive router state. Disabling video routes
  leaves it inert; removing it does not mutate node-owned media.
- Tailscale Serve `:8080` is an additive tailnet-only listener and can be
  removed without touching the existing `:8888` chat front door.
- Release publication occurs only from the final tested merge commit; a failed
  external publishing setup leaves the GitHub prerelease marked accordingly
  and never reuses an existing version.
