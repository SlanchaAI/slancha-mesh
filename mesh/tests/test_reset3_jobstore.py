"""Lease boundary and stale finalization regressions for the SQLite store."""

import pytest

from mesh.job_store import VideoJobStore


@pytest.fixture
def job_store(tmp_path):
    now = [1_000.0]
    store = VideoJobStore(tmp_path / "jobs.sqlite3", clock=lambda: now[0])
    return store, now


def test_delete_claim_can_be_replaced_at_exact_expiry(job_store):
    store, now = job_store
    job = store.create(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="test-specialist",
        owner_node_id="test-node",
        owner_origin="http://test-node.invalid:8091",
        upstream_job_id="test-upstream",
        status="queued",
    )
    first = store.claim_delete(job.public_id, lease_s=10.0)
    assert first is not None

    contender = VideoJobStore(store.path, clock=lambda: now[0])
    now[0] = 1_009.999
    assert contender.claim_delete(job.public_id, lease_s=10.0) is None
    now[0] = 1_010.0
    fresh = contender.claim_delete(job.public_id, lease_s=10.0)
    assert fresh is not None
    assert fresh.job == job
    assert fresh.token != first.token
    assert contender.claim_delete(job.public_id, lease_s=10.0) is None


@pytest.mark.parametrize("cleanup", [False, True], ids=["delete", "cleanup"])
def test_stale_claim_cannot_finalize_newer_claim(job_store, cleanup):
    store, now = job_store
    create = store.enqueue_cleanup if cleanup else store.create
    job = create(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="test-specialist",
        owner_node_id="test-node",
        owner_origin="http://test-node.invalid:8091",
        upstream_job_id="test-upstream",
        status="queued",
    )
    claim_args = () if cleanup else (job.public_id,)
    claim = store.claim_cleanup if cleanup else store.claim_delete
    stale = claim(*claim_args, lease_s=10.0)
    assert stale is not None

    successor = VideoJobStore(store.path, clock=lambda: now[0])
    reclaim = successor.claim_cleanup if cleanup else successor.claim_delete
    now[0] = 1_011.0
    fresh = reclaim(*claim_args, lease_s=10.0)
    assert fresh is not None
    assert fresh.job == job
    assert fresh.token != stale.token

    assert store.confirm_delete(job.public_id, stale.token) is False
    assert reclaim(*claim_args, lease_s=10.0) is None
    assert successor.confirm_delete(job.public_id, fresh.token) is True
    assert successor.confirm_delete(job.public_id, fresh.token) is False
    assert successor.get(job.public_id) is None
