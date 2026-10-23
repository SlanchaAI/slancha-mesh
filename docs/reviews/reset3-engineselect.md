# Engine-selection test backfill

Fleet row: cz2cfad2973d
Target: SlanchaAI/slancha-mesh:mesh/engine_select.py
Source: origin/main at `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Callers and coverage gaps

- `mesh/cli.py:202`: onboarding plan consumes the recommendation; line 217
  uses `installed` to decide whether to suggest installing the backend.
- `mesh/scripts/mesh_doctor.py:446`: readiness check consumes the recommendation
  and reports whether the recommended engine is installed.
- Existing `mesh/tests/test_engine_select.py` covers 24 GB and 8 GB, but not
  adjacent floating-point values around the 24 GB threshold. Its 24 GB check
  accepts either quantization. The new boundary group fixes expectations for
  backend, quantization, alternates, and installation state below/at/above
  24 GB, using `math.nextafter` for the immediate neighbors.
- Existing tests do not exercise an unrecognized, non-null compute capability.
  The new case asserts the existing vLLM/AWQ fallback on Linux. This is distinct
  from `None`, which signifies no CUDA and already has CPU-fallback coverage.
- Existing installation coverage checks Apple Silicon backend and boolean only.
  The new group exercises both sides of the discrete NVIDIA memory threshold
  with absent, unrelated, recommended-only, and multiple installed backends.
  It compares the complete recommendation, allowing only `installed` to change.

All new cases inject synthetic `NodeProbe` values and explicit `os_name="Linux"`.
No hardware probes, engine startup, live services, or recommendation changes.
Three test groups expand to six pytest cases.

## Executed commands and results

Commands ran from the row worktree unless otherwise stated.

- `gh pr list --repo SlanchaAI/slancha-mesh --search cz2cfad2973d --json number,url,title,body`
  returned `[]` before work began.
- `git rev-parse HEAD` returned the source SHA above before edits.
- `rg -n 'recommend_engine' . --glob '*.py'` located the two production callers
  above, the existing recommendation tests, and mocked doctor consumers.
- `uv run --extra dev pytest -q mesh/tests/test_engine_select.py`
  on the clean source: exit 0, **11 passed in 0.02s**.
- `uv run --extra dev pytest -q mesh/tests/test_reset3_engineselect.py`:
  exit 0, **6 passed in 0.01s**.

- Row proof: `test -s docs/reviews/reset3-engineselect.md && (test ! -f mesh/tests/test_reset3_engineselect.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_engineselect.py))`
  returned exit 0, **6 passed in 0.01s**.
- `git diff --check` returned exit 0.

## Remaining findings and limits

No production defect surfaced in these cases. Tests preserve existing decisions;
this review does not validate hardware support claims or engine performance.
The full suite and live CLI/doctor paths were not run; those paths probe hardware
and services outside this synthetic test scope.

GitNexus query/detect-changes tools were absent from the session tool catalog.
No existing symbol was edited. Direct source/caller inspection and Git diff
inspection substitute for unavailable service checks under the fleet fail-open
policy. Only the requested test and this review are included.
