# Reservation coverage review

Fleet row: c43605154085. Source: `SlanchaAI/slancha-mesh:mesh/gpu/reservations.py`.
Inspected default-branch SHA: `2bf33053294ab181ba0d4fd3d11c234332cee28c`.

## Source and callers

`Reservation.to_json` (line 77) emits ISO timestamps; `from_json` (line 84)
first copies its input and parses timestamps. `is_expired` (line 91) includes
exact expiry; `remaining_s` (line 95) clamps elapsed reservations to zero.

Actual callers inspected:

- `mesh/gpu/reservations.py:197`: `ReservationStore._write` serializes records.
- `mesh/gpu/reservations.py:178`: `ReservationStore.list_active` deserializes records.
- `mesh/gpu/cluster.py:259`: `build_cluster_view_from_heartbeats` deserializes
  reservations received in a heartbeat payload.
- `mesh/gpu/cli.py:98`: status output reads `remaining_s` for displayed minutes.
- No production caller of `is_expired` was found; store pruning compares
  timestamps directly at `mesh/gpu/reservations.py:181`.

## Existing coverage and additions

`mesh/tests/test_gpu.py:92` covers a complete UTC record round trip.
`mesh/tests/test_gpu.py:109` checks hour-old/hour-future expiry against the
wall clock and only a lower bound on positive remaining time. Lines 126–185
cover temporary-directory store operations, pruning and totals; these were
inspected, not expanded. Existing probe tests use mocked discovery or a fake
executable; no actual hardware is required.

Three focused cases added in `mesh/tests/test_reset4_gpu_reservations.py`:

1. Explicit wire-format preservation through JSON encoding/decoding, including
   non-UTC offset, microseconds, every field, and unchanged input mapping.
2. Missing optional `purpose`/`pid` fields receive defaults and serialize as
   empty string/null, without adding fields to the input mapping.
3. A mocked UTC clock checks one microsecond before, exactly at, and one
   microsecond after expiry. Assertions cover both the inclusive expired
   boundary and exact remaining seconds, including the zero clamp.

Five collected test instances; synthetic records only. Production code unchanged.
No services, actual reservation records, hardware or reservation manager were
used by the new tests. No defect found in the exercised behavior.

## Verification

- `uv run --extra dev pytest -q mesh/tests/test_reset4_gpu_reservations.py mesh/tests/test_gpu.py`
  — **26 passed**, including five new instances and 21 existing tests.
- Row proof:
  `test -s docs/reviews/reset4-gpu-reservations.md && (test ! -f mesh/tests/test_reset4_gpu_reservations.py || (uv run --extra dev pytest -q mesh/tests/test_reset4_gpu_reservations.py))`
  — **5 passed**, exit 0.

GitNexus MCP tools were unavailable and the repository's referenced guide skill
was absent. Under the global fail-open rule, direct source/caller inspection and
Git diff review substitute for index-based change detection. No existing symbol
was modified; only the two row-authorized files were added.
