# Allocator hard-filter coverage review

Fleet row: c4254471b35a. Source: `SlanchaAI/slancha-mesh:mesh/allocator.py`.
Inspected default-branch SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Call path and existing coverage

- `mesh/cli.py:89` (`auto_select`) calls `allocate_cluster` with a probe and
  catalog, defaulting to `best_per_machine`; `mesh/cli.py:205` also uses it
  for planning. `mesh/registry.py:530` supplies heartbeat hardware snapshots.
- `mesh/allocator.py:257` calls `_rank_specs_for_node`, which invokes
  `model_fit_score` at `mesh/allocator.py:215`. Hard filters at lines 154–160
  reject unsupported backends, insufficient disk, and insufficient effective
  memory. Disk and memory comparisons are strict `>`, so equality fits.
- `mesh/tests/test_allocator.py:38` directly checks unsupported-backend
  rejection. `test_allocate_full_set_two_nodes` at line 95 already checks
  through `allocate_cluster` that a synthetic node lacking the required
  backend has no primary. No redundant backend case was added.
- Existing storage and memory tests at lines 45 and 51 exercise shortages,
  not equality. Remaining allocator tests cover strategy selection,
  coverage diversification, secondary placement, and throughput estimates.
  `mesh/tests/test_e2e.py` exercises synthetic allocation and routing, but
  does not check these resource boundaries.

## Added cases

`mesh/tests/test_reset4_allocator.py` adds two parameterized cases through
`allocate_cluster(..., strategy="best_per_machine")`:

1. Disk free equals the synthetic card's 4 GB storage requirement: assigned
   with a finite score. At 3.75 GB, no primary, negative-infinite score, and
   no alternates.
2. Unified available RAM is 16 GB: after the 8 GB OS reserve, effective
   memory equals the card's 8 GB minimum and assignment succeeds. At
   15.75 GB available, assignment is rejected with the same no-fit checks.

The other resource remains plentiful in each case, isolating each filter.
Tests reuse the synthetic `spark_node` fixture and construct a synthetic
card; no catalog files, hardware probes, running services, real records,
network requests, or model weights are used by the new tests. The allocator
is pure; its dependencies are in-memory inputs, so no I/O mocks are needed.
Production source and existing tests remain unchanged. No defect found in
these cases; no production fix or defect reproduction is required.

## Validation

- `uv run --extra dev pytest -q mesh/tests/test_reset4_allocator.py mesh/tests/test_allocator.py`
  — 33 passed (2 new cases and 31 existing tests).
- Row proof: `test -s docs/reviews/reset4-allocator.md && (test ! -f mesh/tests/test_reset4_allocator.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_allocator.py))`
  — passed; 2 tests passed.
- `git diff --cached --check` — passed.
- GitNexus CLI query confirmed the registry → allocator execution path.
  `detect-changes --scope staged --repo slancha-mesh` ran but reported no
  changes: the registered index targets the source checkout, not this
  isolated worktree. This is not evidence of worktree coverage. Manual
  staged-diff review confirms only the two allowed new files; no existing
  function, class, method, or production execution flow was modified.
