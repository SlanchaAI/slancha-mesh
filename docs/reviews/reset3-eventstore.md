# Registry persistence review

Fleet row: cz71c88ce605
Target: SlanchaAI/slancha-mesh:mesh/event_store.py
Source: origin/main at `2bf33053294ab181ba0d4fd3d11c234332cee28c`

## Result

Review-only delivery: a later failed heartbeat append changes previously
accepted snapshot state. This is a production defect outside this row's
test-only scope. No production changes or knowingly failing tests were added.

`MeshRegistry.record_heartbeat` updates `_node_urls` at
`mesh/registry.py:295` before `_record` calls the store at
`mesh/registry.py:261`. If an accepted heartbeat omitted its URL, snapshot
construction falls back to this mutable cache (`mesh/registry.py:468`).
A subsequent heartbeat supplying a URL changes that cache even when its
append raises. The event list and durable log remain unchanged, but the
snapshot exposes the rejected URL. Restarting restores the original snapshot.

Caller chain: `record_heartbeat` (`mesh/registry.py:292`) → `_record`
(`mesh/registry.py:254`) → `EventStore.append` (`mesh/event_store.py:59`).
Construction consumes `EventStore.replay` at `mesh/registry.py:248`;
`snapshot` (`mesh/registry.py:423`) reads the in-memory model.

## Coverage gaps

- `mesh/tests/test_event_store.py:94` and `:107` cover replay state/order,
  using a list-returning store. They do not check generator exhaustion during
  construction or prohibit subsequent replay calls.
- `mesh/tests/test_event_store.py:119` checks first-append failure with
  `record_node_left` and an empty event list. It misses accepted heartbeat state
  followed by a failed write and the URL-cache mutation demonstrated below.
- `mesh/tests/test_registry.py:105` checks snapshots preserve event count with
  the default null store. It does not observe durable append calls.

The synthetic reproduction below also checks construction exhausts replay and
three subsequent snapshots make no append or replay calls. Those checks pass.
Per the row's defect fallback, evidence replaces a new test module.

## Exact executed validation

At the source revision, before adding this review:

```sh
uv run --extra dev pytest -q mesh/tests/test_event_store.py mesh/tests/test_registry.py
```

Result: exit 0; **22 passed in 0.06s**.

Executed from the worktree, using only synthetic fixtures and an in-memory
store (no services):

```sh
uv run --extra dev python - <<'PY'
from mesh.registry import HeartbeatPostRequest, MeshRegistry
from mesh.tests.conftest import fresh_now, make_heartbeat, spark_node

class Store:
    def __init__(self):
        self.events = []
        self.fail = False
        self.append_calls = 0
        self.replay_calls = 0
        self.yielded = 0
        self.exhausted = 0
    def append(self, env):
        self.append_calls += 1
        if self.fail:
            raise RuntimeError('synthetic append failure')
        self.events.append(env)
    def replay(self):
        self.replay_calls += 1
        for env in tuple(self.events):
            self.yielded += 1
            yield env
        self.exhausted += 1

now = fresh_now.__wrapped__()
node = spark_node.__wrapped__()
hb = make_heartbeat(node, now, [], [])
store = Store()
reg = MeshRegistry(store=store, clock=lambda: now)
reg.record_heartbeat(HeartbeatPostRequest(heartbeat=hb))
before = reg.snapshot(now=now)
accepted = reg.events
persisted = list(store.events)
store.fail = True
try:
    reg.record_heartbeat(HeartbeatPostRequest(heartbeat=hb, node_url='http://rejected.invalid/v1'))
except RuntimeError as exc:
    assert str(exc) == 'synthetic append failure'
else:
    raise AssertionError('append should fail')
after = reg.snapshot(now=now)
assert reg.events == accepted and store.events == persisted
assert before.nodes[node.node_id].node_url is None
assert after.nodes[node.node_id].node_url == 'http://rejected.invalid/v1'
print('DEFECT: failed heartbeat changed accepted snapshot URL: None ->', after.nodes[node.node_id].node_url)

restarted = MeshRegistry(store=store, clock=lambda: now)
assert (store.replay_calls, store.yielded, store.exhausted) == (2, 1, 2)
counters = (store.append_calls, store.replay_calls, store.yielded, store.exhausted)
for _ in range(3):
    assert restarted.snapshot(now=now) == before
assert counters == (store.append_calls, store.replay_calls, store.yielded, store.exhausted)
print('PASS: replay exhausted during construction; three snapshots made no append/replay calls')
print('PASS: restart restores accepted snapshot, exposing divergence from failed writer')
PY
```

Result: exit 0; defect assertion reproduced the rejected URL; iterator,
read-only snapshot, and restart assertions passed. Exit 0 confirms the
reproduction, not correctness of failed-write behavior.

Row proof:

```sh
test -s docs/reviews/reset3-eventstore.md && (test ! -f mesh/tests/test_reset3_eventstore.py || (uv run --extra dev pytest -q mesh/tests/test_reset3_eventstore.py))
```

Result: exit 0 (review-only branch of proof).

`gitnexus_detect_changes` was attempted: exit 127, command not found.
No GitNexus connector or CLI was available, and the referenced local guide
was absent. Services fail open under the global instructions. Manual scope
review substitutes here; only this review file changes, with no symbol edits.

## Remaining findings and lesson

Production fix remains separate: make heartbeat auxiliary cache updates
consistent with successful persistence, then add a regression asserting full
snapshot equality after a failed append. Identity pinning also precedes
persistence (`mesh/registry.py:294`); its failure semantics need separate
investigation and were not reproduced here.

Failure: checking only event-list equality misses observable state changes.
Cause: auxiliary URL state mutates before the durable append. Fix direction:
validate all caller-visible state across failed writes. Tell: a snapshot
changes after a rejected write while restarting restores accepted state.

Validation note: `git diff --cached --check` initially rejected Markdown
hard-break trailing spaces. Removed them; the repeated check passed. Use
blank lines instead of trailing spaces for metadata separation.
