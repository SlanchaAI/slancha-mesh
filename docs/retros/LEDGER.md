# Retrospective proposal ledger

## 2026-08-02 — video ownership cleanup state machine

- **Disposition:** `automate`
- **Evidence:** three review cycles found independent invalid transitions in
  the SQLite cleanup outbox: duplicate exact-owner rows, an expiry transition
  colliding with a pending row, and quarantined work suppressing a fresh enqueue.
- **Target and trigger:** add a property/state-machine test at
  `mesh/tests/test_video_job_store_state_machine.py` whenever cleanup ownership
  schema or transitions change.
- **Minimal reusable content:** generate active, pending, leased, expired,
  confirmed, and quarantined rows; assert one routable owner or one cleanup row
  per exact upstream owner, no queue starvation, and restart-safe leases.
- **Producer and live caller:** `VideoJobStore` produces transitions; the Mesh
  router lifespan cleanup worker claims and consumes them.
- **Pressure scenario:** interleave active creation, compensation enqueue,
  expiry, quarantine, lease timeout, and confirmation for the same
  `(protocol_id, owner_origin, upstream_job_id)` across two store instances.
- **Risk and verification:** test-only automation may expose more transition
  bugs but must not alter runtime state. Verify deterministic seeded runs,
  concurrent SQLite instances, full router tests, and a restart cleanup proof.

Proposal only; no skill or policy changed during the build.
