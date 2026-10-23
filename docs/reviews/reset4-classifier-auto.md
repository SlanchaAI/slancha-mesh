# Classifier auto-routing coverage review

Fleet row: c45ae46162b3.
Source: SlanchaAI/slancha-mesh:mesh/classifier/auto.py.
Inspected origin/main SHA: 2bf33053294ab181ba0d4fd3d11c234332cee28c.

## Call path and existing coverage

The chat-completions route invokes `auto_router.select(body, ready_snapshot)`
through `run_in_threadpool` at `mesh/router_app.py:2708-2709`.
`AutoRouter.select` calls `prompt_text` at `mesh/classifier/auto.py:75`, then
uses either classifier signals or the deterministic general/medium fallback
with the real `select_mesh_route` selector.

`mesh/tests/test_classifier_e2e.py:101` already checks last-user precedence,
a text-parts list, missing messages, and assistant-only messages. That entire
module is gated by four optional classifier dependencies (lines 15-18), so
those assertions are skipped when the inference extra is absent.
`mesh/tests/test_router_auto.py:308` exercises system-only input through the
real AutoRouter, checking general routing without invoking a classifier.
The same module covers HTTP auto resolution, model rewriting, streaming,
missing resolver, cloud fallback, explicit model bypass, and model listing.
Absent-user behavior therefore already has coverage; no duplicate case added.

## Added cases

1. Mixed text/image parts in the latest user turn: joins text in order with
   a newline, excludes image content, earlier user text, system text and a
   later assistant reply. Fake heads receive exactly that text once; the
   real selector returns the synthetic code specialist and endpoint.
2. Latest user turn contains only an image after an earlier text question:
   extraction stays empty rather than reusing older text. Fake heads are
   never called; the real selector returns the synthetic general/medium
   specialist and endpoint despite an available code specialist.

Both cases execute `AutoRouter.select` using a synthetic RegistrySnapshot
and mocked heads. No inference, running services, real records, or network
requests are involved. Production files are unchanged. No defect observed
in these cases; no production fix or defect reproduction is needed.

## Verification

- `uv run --extra dev pytest -q mesh/tests/test_reset4_classifier_auto.py mesh/tests/test_router_auto.py`
  — 13 passed in 0.37s (2 new tests and 11 nearest existing tests).
- Row proof: `test -s docs/reviews/reset4-classifier-auto.md && (test ! -f mesh/tests/test_reset4_classifier_auto.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_classifier_auto.py))`
  — passed; 2 tests passed.

GitNexus tools are not exposed in this worker, its CLI is absent, and the
repository-referenced GitNexus skill is missing. Following the global
fail-open rule, inspected source/callers directly and checked the outgoing
diff for scope. No existing functions, classes, or methods were modified.
