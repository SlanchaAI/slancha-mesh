# Runtime health test backfill

Fleet row: cz4b4de70ca5.
Source: SlanchaAI/slancha-mesh:mesh/runtime_health.py at origin/main
`2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Existing coverage and callers

- `mesh/tests/test_runtime_health.py:19` already checks reservation at exact
  cooldown; line 42 checks a failed probe restarting cooldown; line 92 checks
  that peeking does not consume the probe.
- `mesh/tests/test_router_app.py:1009` exercises the actual chat endpoint at
  exact cooldown using synthetic discovery and an HTTP mock transport. Its
  successful request restores reported routable capacity. No duplicate test added.
- `mesh/router_app.py:1275` reserves streaming chat attempts with `is_routable`;
  `mesh/router_app.py:1632` reserves media requests through the same method.
  `mesh/router_app.py:2911` uses `peek_routable` for health reporting.
- `mesh/tests/test_runtime_health.py:71` checks map size but does not assert
  which binding survives eviction. Existing probe tests use sequential callers.

## Added coverage

`mesh/tests/test_reset3_runtimehealth.py` adds three isolated cases:

1. A newer closed binding is evicted before an older open binding. The open
   binding remains unroutable and the map remains bounded.
2. Equal-age closed bindings evict by key order, even when insertion order
   would select a different binding.
3. Eight barrier-synchronized callers at exact cooldown reserve exactly one
   half-open probe. Peeking and further reservations remain blocked until that
   probe succeeds; success restores routability.

Clocks are synthetic. Barrier waits and thread joins are bounded, worker
exceptions reach the test through futures, and workers are daemon threads so
an unexpected deadlock cannot keep the process alive. No sleeps or services.
Production code is unchanged.

## Executed checks

Commands run from the row worktree:

- `uv run --extra dev pytest -q mesh/tests/test_runtime_health.py`
  — 5 passed on clean source before adding tests.
- `uv run --extra dev pytest -q mesh/tests/test_reset3_runtimehealth.py`
  — 3 passed.
- `uv run --extra dev pytest -q mesh/tests/test_runtime_health.py mesh/tests/test_router_app.py`
  — 38 passed, including the existing endpoint cooldown test.
- `uv run --extra dev ruff check mesh/tests/test_reset3_runtimehealth.py`
  — all checks passed.

## Remaining findings

No production defect observed in these cases. The concurrent test controls
arrival with a barrier; it is a regression check, not exhaustive exploration
of all thread schedules. All-open map eviction and probe cancellation remain
outside this backfill.

GitNexus MCP tools and the referenced repository skill files are unavailable.
The locally cached CLI reports this worktree is not indexed; source caller
inspection and scoped diff review provide the fallback. No existing symbols
were edited.

Final validation:

- `test -s docs/reviews/reset3-runtimehealth.md && (test ! -f mesh/tests/test_reset3_runtimehealth.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_runtimehealth.py))`
  — exit 0; 3 passed.
- `git diff --cached --check` — exit 0.
- `npx --no-install gitnexus status` — repository not indexed.
- `npx --no-install gitnexus detect-changes --scope staged` — unavailable:
  multiple registered repositories, none selected.
- `npx --no-install gitnexus detect-changes --scope staged --repo slancha-mesh`
  — exit 1: repository not found in the index registry. No index generated;
  the two-file scope and source inspection are the fallback verification.
