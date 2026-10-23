# Classifier heads coverage audit

Fleet row: c4be28bb3054. Source: `SlanchaAI/slancha-mesh:mesh/classifier/heads.py`.
Inspected default-branch SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Finding and disposition

The requested unknown-cap and low-confidence cases already have tests through
`SignalClassifier.signals_for`. Deliver this coverage audit under the row's
explicit review-only alternative; no redundant tests or production changes.
No production defect was established, so there is no defect reproduction.

## Caller and behavior

The production caller is `mesh/classifier/auto.py:85`, in `AutoRouter.select`.
It passes the returned signals to `select_mesh_route` at line 86 and records
`cluster_hint` at line 96. `mesh/classifier/heads.py:143` also calls
`signals_for` during warmup.

`signals_for` invokes `_apply_cluster_override` at `mesh/classifier/heads.py:172`.
The selector rejects low-confidence predictions and unmapped cluster IDs;
the classifier rejects unknown sidecar capabilities. Each rejection preserves
the six-head domain and returns no cluster hint.

## Existing coverage

- `mesh/tests/test_classifier_heads.py:61` supplies a fake cluster head with
  confidence 0.99 and unknown capability `quantum`, then asserts the domain
  matches the baseline and the cluster hint is absent.
- `mesh/tests/test_classifier_heads.py:70` supplies a fake cluster head with
  confidence 0.1 and mapped capability `coding`, with the same assertions.
- `mesh/tests/test_classifier_cluster_head.py:42` checks rejection just below
  the confidence threshold; line 47 checks an unmapped cluster ID.
- Positive controls exist at `mesh/tests/test_classifier_heads.py:53` for
  the domain override and `mesh/tests/test_classifier_cluster_head.py:35`
  for the mapped selector hint.

The heads module uses a fake embedder but loads real vendored Treelite heads.
It is therefore not an entirely mocked test suite. The selector module uses
synthetic NumPy arrays and fake heads. Unknown cluster IDs have selector-level
coverage, not a dedicated `signals_for` integration test. An entirely mocked
`signals_for` suite would improve optional-dependency independence, but the
requested rejection semantics already have coverage; this row explicitly
permits an audit instead of duplicating that coverage. New cases added: none.

## Executed verification

1. `uv run --extra dev pytest -q mesh/tests/test_classifier_heads.py mesh/tests/test_classifier_cluster_head.py`
   returned exit 5: two modules skipped because NumPy is not in the dev extra.
   This is not a passing test run.
2. `uv run --extra dev --with numpy pytest -q mesh/tests/test_classifier_heads.py mesh/tests/test_classifier_cluster_head.py`
   returned exit 0: **10 passed, 1 skipped**. The selector module ran; the
   heads module skipped because Treelite is absent. NumPy was supplied only
   to the verification environment; project dependencies were not changed.
   No trained model was loaded, downloaded, or used for inference. The
   corrupt-artifact test falls through on the unavailable Treelite import.
3. Row proof:
   `test -s docs/reviews/reset4-classifier-heads.md && (test ! -f mesh/tests/test_reset4_classifier_heads.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_classifier_heads.py))`
   passed (exit 0), using the review-only branch. The new-module pytest command
   is intentionally inapplicable because no new test module was added.

All test data was synthetic; no running services or real records were used.
The heads assertions above were inspected, not executed against real models.
GitNexus MCP tools and CLI were unavailable (no matching tool; CLI command
not found). Scope was checked with Git diff; only this review is changed.
