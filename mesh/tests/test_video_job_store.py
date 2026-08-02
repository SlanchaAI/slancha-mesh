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
