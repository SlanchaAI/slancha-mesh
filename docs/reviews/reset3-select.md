# Selection boundary test backfill

Fleet row cz3b2f2e38c0 targets `SlanchaAI/slancha-mesh:mesh/select.py`.
Source inspected: origin/main at `2bf33053294ab181ba0d4fd3d11c234332cee28c`.
Production behavior is unchanged. Only this review and
`mesh/tests/test_reset3_select.py` are added.

## Callers and source evidence

- `mesh/classifier/auto.py:81` and `mesh/classifier/auto.py:86` call
  `select_mesh_route` for classifier-driven routing.
- `packages/slancha-mesh-tune/mesh/scripts/mesh_replay.py:136` calls the same API
  for replay. No non-test in-repository caller of `select_mesh_route_with_pref`
  was found; its no-preference branch delegates at `mesh/select.py:413`.
- `mesh/registry.py:558` produces candidate lists from snapshot specialists.
  The tests supply synthetic pre-ranked snapshots directly to the public APIs;
  they do not exercise registry discovery, inference, or live services.
- `mesh/select.py:98` and `mesh/select.py:101` reject queue and hot-interactive
  latency values strictly above their budgets. `mesh/select.py:323` applies
  the preference latency ceiling. `mesh/select.py:278` rejects a missing card;
  `mesh/select.py:282` requires all requested capabilities.
- `mesh/select.py:176` and `mesh/select.py:475` sort by score only. Ties retain
  input candidate order. Deterministic here means repeatable for the same
  ordered snapshot, not canonical ordering across differently ordered snapshots.

## Coverage gaps filled

Existing `mesh/tests/test_select.py` covers large queue/latency overruns, and
`mesh/tests/test_select_with_pref.py:119` covers one required capability.
Neither selector module explicitly checks the following four cases:

1. Both requested capabilities must exist: empty cards, either partial set,
   missing cards, and a complete set (five cases).
2. Queue equality passes; one millisecond over fails. Exercise zero, custom
   17 ms, and 2000 ms limits through both APIs (twelve cases).
3. Exact hot-interactive 1500 ms and preference 800 ms latency limits pass;
   one millisecond over fails, including the preference-enabled base filter
   (six cases).
4. Equal-score winners and the entire fallback chain retain candidate order
   across all six permutations of three candidates, for both APIs; repeated
   calls agree and do not reorder the input (twelve cases).

Fixtures are isolated synthetic cards/routes with fixed timestamps and `.invalid`
URLs. No shared catalog fixtures or private selector helpers are used.

## Executed validation

Commands ran in the row worktree on Dell:

- Before edits: `uv run --extra dev pytest -q mesh/tests/test_select.py mesh/tests/test_select_with_pref.py`
  — exit 0, 26 passed.
- `uv run --extra dev pytest -q mesh/tests/test_reset3_select.py`
  — exit 0, 35 passed.
- `uv run --extra dev ruff check mesh/tests/test_reset3_select.py`
  — exit 0, all checks passed.

- Final `uv run --extra dev pytest -q mesh/tests/test_select.py mesh/tests/test_select_with_pref.py`
  — exit 0, 26 passed.
- Row proof: `test -s docs/reviews/reset3-select.md && (test ! -f mesh/tests/test_reset3_select.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_select.py))`
  — exit 0, 35 passed.
- Final `uv run --extra dev ruff check mesh/tests/test_reset3_select.py`
  — exit 0, all checks passed.
- `git diff --check` — exit 0.

## Remaining findings

No production defect was demonstrated in these cases. Tie ordering intentionally
records existing stable-input behavior; canonical ordering would need a separate
policy decision and production change. Preference selection has no production
caller in this repository; these tests do not establish deployment wiring.
GitNexus MCP tools and the `gitnexus` executable were unavailable, and the
AGENTS.md-referenced `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` was absent.
Source/caller inspection and the scoped diff substitute for unavailable graph
checks under the global services-fail-open rule. No existing symbol was edited.

Review lesson: guessed source offsets drifted during drafting; corrected them
from `rg -n` output. Verify exact source anchors before recording evidence.
