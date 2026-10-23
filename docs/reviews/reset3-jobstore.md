# Job-store lease boundary review

Fleet row: `czcaaee676ff`.
Source: `SlanchaAI/slancha-mesh:mesh/job_store.py` at origin/main
`2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Findings and coverage

Three missing cases now pass in `mesh/tests/test_reset3_jobstore.py`:

- An ordinary delete lease excludes a second store instance just before its
  deadline and permits a new claim exactly at the deadline.
- An old ordinary delete claim cannot finalize a newer claim.
- An old cleanup claim cannot finalize a newer claim.

The stale-finalization cases check that the replacement lease remains exclusive
and that its token can still delete the row exactly once. All cases use public
store methods, temporary SQLite databases, synthetic owners, and fixed clocks.
No worker or live service runs. Production behavior is unchanged.

Existing coverage was retained rather than duplicated:

- `mesh/tests/test_video_job_store.py:134` covers ordinary delete lease recovery
  after expiry and stale release rejection, but not exact expiry or stale
  `confirm_delete` rejection.
- `mesh/tests/test_video_job_store.py:357` already covers cleanup lease exclusion
  immediately before expiry and recovery at the exact deadline.
- `mesh/tests/test_video_job_store.py:196` covers expired public owners becoming
  hidden cleanup work, including eventual confirmed deletion.
- `mesh/tests/test_video_job_store.py:229` covers an enqueued cleanup job remaining
  hidden from ordinary `get`, with its owner retained across store restart.

## Caller evidence

- `mesh/router_app.py:1829`: public lookup calls `store.get`; hidden rows become
  a 404 rather than a public owner response.
- `mesh/router_app.py:1850`: the delete route claims an ordinary delete lease.
- `mesh/router_app.py:1933`: the cleanup loop claims pending work;
  `mesh/router_app.py:1950` confirms deletion using that claim's token.
- `mesh/router_app.py:2162`: failed-create compensation enqueues cleanup work.
- `mesh/router_app.py:2615`: the public delete route confirms its leased delete.

## Executed checks

Commands ran from the isolated worktree on Dell:

```text
uv run --extra dev pytest -q mesh/tests/test_video_job_store.py
23 passed in 0.29s (clean source baseline)

uv run --extra dev pytest -q mesh/tests/test_reset3_jobstore.py
3 passed in 0.04s

test -s docs/reviews/reset3-jobstore.md && (test ! -f mesh/tests/test_reset3_jobstore.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_jobstore.py))
3 passed in 0.04s; exit 0 (row proof)

git diff --check
exit 0
```

## Remaining findings

No production defect found in the requested cases. This review does not establish
worker/network behavior or concurrent-process scheduling; those are outside this
test-only row. Existing threaded claim coverage remains in the nearest module.
GitNexus tools were unavailable in this session, and the repository's referenced
GitNexus skill file was absent. Direct source/caller inspection and Git diff scope
checks supplied the fallback; no existing symbol was edited.
