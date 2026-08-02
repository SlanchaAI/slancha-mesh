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
    assert remaining == 1
    assert store.cleanup_expired() == 1


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
