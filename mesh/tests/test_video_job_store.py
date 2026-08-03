"""Durable ownership records for asynchronous video jobs."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from mesh.job_store import VideoJobStore


def _create(store: VideoJobStore, index: int = 0):
    return store.create(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id=f"node-{index}",
        owner_origin=f"http://node-{index}:8091",
        upstream_job_id=f"video_gen_upstream_{index}",
        status="queued",
    )


def test_crud_persists_only_routing_ownership(tmp_path) -> None:
    db_path = tmp_path / "router" / "video-jobs.sqlite3"
    store = VideoJobStore(db_path)

    created = _create(store)

    assert created.public_id.startswith("video_")
    assert len(created.public_id) == 38
    assert set(created.public_id[6:]) <= set("0123456789abcdef")
    assert store.get(created.public_id) == created

    updated = store.update_status(created.public_id, "in_progress")
    assert updated is not None
    assert updated.status == "in_progress"
    assert updated.updated_at >= created.updated_at

    assert store.delete(created.public_id) is True
    assert store.get(created.public_id) is None
    assert store.delete(created.public_id) is False

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(video_jobs)")
        }
    assert columns == {
        "public_id",
        "protocol_id",
        "specialist_id",
        "owner_node_id",
        "owner_origin",
        "upstream_job_id",
        "created_at",
        "updated_at",
        "expires_at",
        "last_status",
        "delete_claim_token",
        "delete_claimed_until",
        "cleanup_pending",
        "cleanup_reason",
    }


def test_expired_records_are_not_found_and_cleanup_is_bounded(tmp_path) -> None:
    now = [1_000.0]
    db_path = tmp_path / "video-jobs.sqlite3"
    store = VideoJobStore(
        db_path,
        ttl_s=10.0,
        cleanup_batch=2,
        clock=lambda: now[0],
    )
    records = [_create(store, index) for index in range(3)]

    now[0] = 1_011.0

    assert store.get(records[0].public_id) is None
    with sqlite3.connect(db_path) as connection:
        remaining = connection.execute("SELECT count(*) FROM video_jobs").fetchone()[0]
        pending = connection.execute(
            "SELECT count(*) FROM video_jobs WHERE cleanup_pending = 1"
        ).fetchone()[0]
    assert remaining == 3
    assert pending == 2
    assert store.cleanup_expired() == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM video_jobs WHERE cleanup_pending = 1"
        ).fetchone()[0] == 3


def test_wal_store_supports_concurrent_app_threads(tmp_path) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(lambda index: _create(store, index), range(40)))

    assert len({record.public_id for record in records}) == 40
    assert all(store.get(record.public_id) is not None for record in records)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_existing_owner_database_is_migrated_for_delete_claims(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE video_jobs (
                public_id TEXT PRIMARY KEY,
                protocol_id TEXT NOT NULL,
                specialist_id TEXT NOT NULL,
                owner_node_id TEXT NOT NULL,
                owner_origin TEXT NOT NULL,
                upstream_job_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                last_status TEXT NOT NULL
            )
            """
        )

    store = VideoJobStore(db_path)
    record = _create(store)

    claim = store.claim_delete(record.public_id)
    assert claim is not None
    assert claim.job == record


def test_delete_claim_is_exclusive_and_recovers_after_bounded_lease(tmp_path) -> None:
    now = [1_000.0]
    store = VideoJobStore(
        tmp_path / "video-jobs.sqlite3",
        clock=lambda: now[0],
    )
    record = _create(store)

    first = store.claim_delete(record.public_id, lease_s=10)

    assert first is not None
    assert first.job == record
    assert store.claim_delete(record.public_id, lease_s=10) is None

    now[0] = 1_011.0
    recovered = store.claim_delete(record.public_id, lease_s=10)
    assert recovered is not None
    assert recovered.token != first.token
    assert store.release_delete(record.public_id, first.token) is False
    assert store.confirm_delete(record.public_id, recovered.token) is True
    assert store.get(record.public_id) is None


def test_failed_delete_claim_can_be_released_for_immediate_retry(tmp_path) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    record = _create(store)
    claim = store.claim_delete(record.public_id)
    assert claim is not None

    assert store.release_delete(record.public_id, claim.token) is True
    retry = store.claim_delete(record.public_id)
    assert retry is not None
    assert retry.token != claim.token


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_id", "p" * 129),
        ("specialist_id", "s" * 257),
        ("owner_node_id", "n" * 257),
        ("owner_origin", "o" * 2049),
        ("upstream_job_id", "u" * 1025),
        ("status", "x" * 65),
    ],
)
def test_text_fields_are_bounded_before_sqlite_write(tmp_path, field, value) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    kwargs = {
        "protocol_id": "vllm_omni.video.jobs.v1",
        "specialist_id": "video-specialist",
        "owner_node_id": "node-0",
        "owner_origin": "http://node-0:8091",
        "upstream_job_id": "video_gen_upstream_0",
        "status": "queued",
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        store.create(**kwargs)


def test_expired_owner_becomes_cleanup_work_until_upstream_delete_is_confirmed(
    tmp_path,
) -> None:
    now = [1_000.0]
    db_path = tmp_path / "video-jobs.sqlite3"
    store = VideoJobStore(db_path, ttl_s=10.0, clock=lambda: now[0])
    record = _create(store)

    now[0] = 1_011.0

    assert store.get(record.public_id) is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM video_jobs WHERE public_id = ?",
            (record.public_id,),
        ).fetchone()[0] == 1

    claim = store.claim_cleanup(lease_s=10.0)
    assert claim is not None
    assert claim.job == record
    assert store.claim_cleanup(lease_s=10.0) is None

    assert store.release_delete(record.public_id, claim.token) is True
    retry = VideoJobStore(db_path, ttl_s=10.0, clock=lambda: now[0]).claim_cleanup(
        lease_s=10.0
    )
    assert retry is not None
    assert retry.job == record
    assert store.confirm_delete(record.public_id, retry.token) is True
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM video_jobs").fetchone()[0] == 0


def test_failed_create_cleanup_outbox_is_hidden_and_survives_restart(tmp_path) -> None:
    db_path = tmp_path / "video-jobs.sqlite3"
    store = VideoJobStore(db_path)

    queued = store.enqueue_cleanup(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id="node-0",
        owner_origin="http://node-0:8091",
        upstream_job_id="video_gen_orphaned",
        status="queued",
    )

    assert store.get(queued.public_id) is None
    claim = VideoJobStore(db_path).claim_cleanup()
    assert claim is not None
    assert claim.job == queued


def test_cleanup_enqueue_is_idempotent_for_one_exact_upstream_owner(tmp_path) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    kwargs = {
        "protocol_id": "vllm_omni.video.jobs.v1",
        "specialist_id": "video-specialist",
        "owner_node_id": "node-0",
        "owner_origin": "http://node-0:8091",
        "upstream_job_id": "video_gen_orphaned",
        "status": "queued",
    }

    first = store.enqueue_cleanup(**kwargs)
    second = store.enqueue_cleanup(**kwargs)

    assert second.public_id == first.public_id
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM video_jobs").fetchone()[0] == 1


def test_cleanup_enqueue_never_hides_an_existing_active_owner(tmp_path) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    active = _create(store)

    found = store.enqueue_cleanup(
        protocol_id=active.protocol_id,
        specialist_id=active.specialist_id,
        owner_node_id=active.owner_node_id,
        owner_origin=active.owner_origin,
        upstream_job_id=active.upstream_job_id,
        status=active.status,
    )

    assert found == active
    assert store.get(active.public_id) == active
    assert store.claim_cleanup() is None


def test_cleanup_outbox_has_schema_level_exact_owner_uniqueness(tmp_path) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    store.enqueue_cleanup(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id="node-0",
        owner_origin="http://node-0:8091",
        upstream_job_id="unique-job",
        status="queued",
    )

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO video_jobs (
                    public_id, protocol_id, specialist_id, owner_node_id,
                    owner_origin, upstream_job_id, created_at, updated_at,
                    expires_at, last_status, cleanup_pending, cleanup_reason
                )
                SELECT 'video_00000000000000000000000000000000', protocol_id,
                       specialist_id, owner_node_id, owner_origin, upstream_job_id,
                       created_at, updated_at, expires_at, last_status, 1, cleanup_reason
                FROM video_jobs
                """
            )


def test_cleanup_enqueue_keeps_same_upstream_id_from_different_owners_distinct(
    tmp_path,
) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    kwargs = {
        "protocol_id": "vllm_omni.video.jobs.v1",
        "specialist_id": "video-specialist",
        "owner_node_id": "node-0",
        "owner_origin": "http://node-0:8091",
        "upstream_job_id": "shared-id",
        "status": "queued",
    }

    first = store.enqueue_cleanup(**kwargs)
    kwargs["owner_node_id"] = "node-1"
    kwargs["owner_origin"] = "http://node-1:8091"
    second = store.enqueue_cleanup(**kwargs)

    assert second.public_id != first.public_id


def test_cleanup_quarantine_retains_owner_but_removes_it_from_work_queue(
    tmp_path,
) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    queued = store.enqueue_cleanup(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id="node-0",
        owner_origin="http://node-0:8091",
        upstream_job_id="untrusted-job",
        status="queued",
    )
    claim = store.claim_cleanup()
    assert claim is not None

    assert store.quarantine_cleanup(queued.public_id, claim.token) is True
    assert store.claim_cleanup() is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT cleanup_pending, cleanup_reason FROM video_jobs"
        ).fetchone() == (2, "invalid_protocol")


def test_deferred_cleanup_recovers_after_its_absolute_lease_deadline(tmp_path) -> None:
    now = [1_000.0]
    store = VideoJobStore(
        tmp_path / "video-jobs.sqlite3",
        clock=lambda: now[0],
    )
    queued = store.enqueue_cleanup(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id="missing-node",
        owner_origin="http://missing-node:8091",
        upstream_job_id="retry-later",
        status="queued",
    )

    first = store.claim_cleanup(lease_s=10.0)
    assert first is not None
    assert store.claim_cleanup(lease_s=10.0) is None

    now[0] = 1_009.999
    assert store.claim_cleanup(lease_s=10.0) is None
    now[0] = 1_010.0
    retry = store.claim_cleanup(lease_s=10.0)
    assert retry is not None
    assert retry.job.public_id == queued.public_id
    assert retry.token != first.token


def test_expiry_merges_active_owner_into_existing_cleanup_row(tmp_path) -> None:
    now = [1_000.0]
    store = VideoJobStore(
        tmp_path / "video-jobs.sqlite3",
        ttl_s=10.0,
        clock=lambda: now[0],
    )
    cleanup = store.enqueue_cleanup(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id="node-0",
        owner_origin="http://node-0:8091",
        upstream_job_id="same-upstream-job",
        status="queued",
    )
    active = store.create(
        protocol_id=cleanup.protocol_id,
        specialist_id=cleanup.specialist_id,
        owner_node_id=cleanup.owner_node_id,
        owner_origin=cleanup.owner_origin,
        upstream_job_id=cleanup.upstream_job_id,
        status="queued",
    )

    now[0] = 1_011.0
    assert store.get(active.public_id) is None

    claim = store.claim_cleanup()
    assert claim is not None
    assert claim.job.upstream_job_id == cleanup.upstream_job_id
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM video_jobs").fetchone()[0] == 1


def test_fresh_enqueue_revives_quarantined_exact_owner(tmp_path) -> None:
    store = VideoJobStore(tmp_path / "video-jobs.sqlite3")
    kwargs = {
        "protocol_id": "vllm_omni.video.jobs.v1",
        "specialist_id": "video-specialist",
        "owner_node_id": "node-0",
        "owner_origin": "http://node-0:8091",
        "upstream_job_id": "retry-after-quarantine",
        "status": "queued",
    }
    queued = store.enqueue_cleanup(**kwargs)
    claim = store.claim_cleanup()
    assert claim is not None
    assert store.quarantine_cleanup(queued.public_id, claim.token) is True

    revived = store.enqueue_cleanup(**kwargs)

    assert revived.public_id == queued.public_id
    retry = store.claim_cleanup()
    assert retry is not None
    assert retry.job.public_id == queued.public_id


def test_cleanup_claim_is_exclusive_across_store_instances(tmp_path) -> None:
    path = tmp_path / "video-jobs.sqlite3"
    store = VideoJobStore(path)
    store.enqueue_cleanup(
        protocol_id="vllm_omni.video.jobs.v1",
        specialist_id="video-specialist",
        owner_node_id="node-0",
        owner_origin="http://node-0:8091",
        upstream_job_id="concurrent-claim",
        status="queued",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: VideoJobStore(path).claim_cleanup(), range(2)))

    assert sum(claim is not None for claim in claims) == 1
