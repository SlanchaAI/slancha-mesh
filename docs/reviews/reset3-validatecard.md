# Validation backfill: served model aliases

Fleet row: cz88ece0d261.
Source: `SlanchaAI/slancha-mesh:mesh/validate_card.py`.
Inspected origin/main SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Callers and schema

- `pyproject.toml:47` exposes `slancha-mesh-validate` through `mesh.validate_card:main`.
- `mesh/validate_card.py:440` calls public `validate_paths` from `main`.
- `mesh/validate_card.py:391` validates each supplied path; line 377 invokes
  the alias scope check, whose rule starts at line 220.
- `mesh/models.py:210` documents adopted endpoints as `required_backend =
  "external"` with `static_base_url`; line 221 declares optional
  `served_model_name`. There is no separate invented binding field.
- `mesh/tests/test_validate_card.py:44` exercises the public validation entrypoint.
  Its malformed-schema and TOML cases at lines 270 and 280 assert error
  presence, without checking complete diagnostic ordering or repeatability.
- Existing router tests (`mesh/tests/test_router_app.py:281` and
  `mesh/tests/test_router_auto.py:226`) cover forwarding aliases, not linter rules.

## Missing coverage added

Four groups in `mesh/tests/test_reset3_validatecard.py`, 16 parameterized cases:

1. An adopted external endpoint accepts a nonempty alias without findings.
2. Each managed backend (vllm, mlx, llamacpp, ollama) emits exactly one warning
   for a nonempty alias, identifying the alias and backend without a hard error.
3. Absent and explicitly empty aliases are clean for external and all four
   managed backends.
4. Multiple schema errors and a TOML parse error retain input-path and schema
   field order, error severity, field/value context, and identical diagnostics
   on repeated calls. Reversing the input reverses file groups; a valid card
   adds no findings. Parser wording is not pinned across Python versions.

All fixtures are synthetic temporary files. Tests call public `validate_paths`
with lists and iterators. The loopback URL is inert fixture data: no service,
network request, model download, or production-code modification is involved.

## Executed checks

Commands executed from the isolated worktree:

- `uv run --extra dev pytest -q mesh/tests/test_validate_card.py`
  on unchanged source: **27 passed in 0.04s**.
- `uv run --extra dev pytest -q mesh/tests/test_reset3_validatecard.py`:
  **16 passed in 0.02s**.
- `uv run --extra dev pytest -q mesh/tests/test_validate_card.py`
  after adding tests: **27 passed in 0.03s**.

## Remaining findings and limits

No production defect found in these cases. Production behavior remains unchanged.
This is linter coverage, not an assertion that an upstream service accepts an alias.
The existing tests already cover single parse/schema errors, so new coverage
focuses on multi-input diagnostics rather than duplicating those assertions.

GitNexus MCP tools, CLI, and the referenced repository skill file are unavailable
in this worker. Automated `gitnexus_detect_changes` could not run; per global
fail-open policy, direct caller inspection and explicit-path diff review substitute.
No existing production symbols were edited. Full suite and live services are
outside this test-only row; the nearest existing validator module passes.

Final checks:

- `uv run --extra dev ruff check mesh/tests/test_reset3_validatecard.py`:
  **All checks passed**.
- Row proof, executed exactly:
  `test -s docs/reviews/reset3-validatecard.md && (test ! -f mesh/tests/test_reset3_validatecard.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_validatecard.py))`:
  **exit 0; 16 passed in 0.02s**.
- `git diff --check`: **exit 0**.
